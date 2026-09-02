"""Per-installation rate limiting for the GitHub API (SPEC §7).

An installation token gets on the order of 5,000 requests an hour. That budget
belongs to the installation, not to us, so a local counter is only ever a guess —
the authority is the `x-ratelimit-*` headers on every response. The bucket here
therefore does two jobs at once: it **paces** requests between responses, and it
**corrects itself** from whatever GitHub last said.

Three things are honoured, and they are not the same thing:

* `x-ratelimit-remaining` — how much of the primary budget is left. Our token
  count is clamped down to it and never up, because a request in flight has
  already been spent as far as we are concerned and not yet as far as GitHub is.
* `x-ratelimit-reset` — the instant the primary window turns over *wholesale*.
  A token bucket refills continuously and GitHub's does not, so the window
  boundary is modelled separately: at that instant the allowance is restored.
* `retry-after` — the secondary limit, which is about request *shape* rather
  than budget and can arrive with thousands of requests still remaining. It is a
  hard block, independent of the bucket.

A wait longer than `max_wait_seconds` is refused rather than slept through. The
worker holds a claimed queue row while it works, and a row held longer than
`reaper_timeout_seconds` is handed to another worker; sleeping out a primary
limit could take the best part of an hour. Handing the wait back to the queue,
which already retries with backoff, is the only version of this that keeps the
reaper's guarantee true.
"""

import asyncio
import time
from dataclasses import dataclass

from app.logging import get_logger

log = get_logger(__name__)

# What an installation token is worth per hour before any response has told us
# otherwise. The first response corrects it.
DEFAULT_LIMIT = 5_000
WINDOW_SECONDS = 3_600.0


class RateLimitExceeded(RuntimeError):
    """The wait GitHub is asking for is longer than this worker will hold a row."""

    def __init__(self, installation_id: int, retry_after: float) -> None:
        super().__init__(
            f"installation {installation_id} is rate limited for another "
            f"{retry_after:.0f}s, longer than this worker will wait"
        )
        self.installation_id = installation_id
        self.retry_after = retry_after


class Clock:
    """Real time. Tests substitute one that advances without waiting.

    Two clocks, deliberately. Elapsed time is measured on the monotonic clock so
    an NTP correction cannot make a bucket refill backwards, while
    `x-ratelimit-reset` is an epoch second and needs the wall clock to be read at
    all. It is converted to a *duration* at the moment it arrives, so a skewed
    local clock costs one wait of the wrong length rather than a permanent offset.
    """

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass
class Bucket:
    """One installation's budget. All instants are monotonic seconds."""

    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float
    # When GitHub's window turns over and restores the whole allowance.
    resets_at: float | None = None
    # Before this, nothing may leave: a spent primary budget or a retry-after.
    blocked_until: float = 0.0

    def advance(self, now: float) -> None:
        if self.resets_at is not None and now >= self.resets_at:
            self.tokens = self.capacity
            self.resets_at = None
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now

    def wait_for_token(self, now: float) -> float:
        """Seconds until a request may leave. Zero means now."""
        self.advance(now)
        wait = max(0.0, self.blocked_until - now)
        if self.tokens < 1.0:
            wait = max(wait, (1.0 - self.tokens) / self.refill_per_second)
        return wait

    def take(self) -> None:
        self.tokens -= 1.0


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _as_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        # `retry-after` may be an HTTP date. GitHub sends seconds; if that ever
        # changes, a missed pause is safer than a crash, because the response
        # itself already told us to back off and the bucket will catch up.
        return None


class RateLimiter:
    """A token bucket per installation, corrected by response headers."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        max_wait_seconds: float = 60.0,
        default_limit: int = DEFAULT_LIMIT,
        window_seconds: float = WINDOW_SECONDS,
    ) -> None:
        self._clock = clock or Clock()
        self._max_wait = max_wait_seconds
        self._default_limit = default_limit
        self._window = window_seconds
        self._buckets: dict[int, Bucket] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def bucket(self, installation_id: int) -> Bucket:
        bucket = self._buckets.get(installation_id)
        if bucket is None:
            limit = float(self._default_limit)
            now = self._clock.monotonic()
            bucket = Bucket(
                capacity=limit,
                refill_per_second=limit / self._window,
                tokens=limit,
                updated_at=now,
            )
            self._buckets[installation_id] = bucket
        return bucket

    def _lock(self, installation_id: int) -> asyncio.Lock:
        lock = self._locks.get(installation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[installation_id] = lock
        return lock

    async def acquire(self, installation_id: int) -> None:
        """Block until this installation may make one request.

        The lock is held across the wait on purpose: waiters queue behind it and
        take their tokens one at a time, so three callers finding one token left
        do not all decide they may proceed.
        """
        async with self._lock(installation_id):
            while True:
                bucket = self.bucket(installation_id)
                wait = bucket.wait_for_token(self._clock.monotonic())
                if wait <= 0.0:
                    bucket.take()
                    return
                if wait > self._max_wait:
                    raise RateLimitExceeded(installation_id, wait)
                log.info(
                    "github.rate_limit_wait",
                    installation_id=installation_id,
                    seconds=round(wait, 3),
                    remaining=round(bucket.tokens, 1),
                )
                await self._clock.sleep(wait)

    def observe(self, installation_id: int, headers: object) -> None:
        """Correct the bucket from one response's headers.

        Takes anything with a case-insensitive `.get`, which is what both httpx
        and a plain dict of lowercase keys provide.
        """
        get = headers.get  # type: ignore[attr-defined]
        bucket = self.bucket(installation_id)
        now = self._clock.monotonic()
        bucket.advance(now)

        limit = _as_int(get("x-ratelimit-limit"))
        if limit is not None and limit > 0:
            bucket.capacity = float(limit)
            bucket.refill_per_second = limit / self._window

        reset = _as_int(get("x-ratelimit-reset"))
        if reset is not None:
            bucket.resets_at = now + max(0.0, reset - self._clock.wall())

        remaining = _as_int(get("x-ratelimit-remaining"))
        if remaining is not None:
            bucket.tokens = min(bucket.tokens, float(remaining))
            if remaining <= 0 and bucket.resets_at is not None:
                bucket.blocked_until = max(bucket.blocked_until, bucket.resets_at)

        retry_after = _as_float(get("retry-after"))
        if retry_after is not None:
            bucket.blocked_until = max(bucket.blocked_until, now + retry_after)

    def headroom(self, installation_id: int) -> float:
        """Requests believed available right now. SPEC §9 reports this."""
        bucket = self.bucket(installation_id)
        bucket.advance(self._clock.monotonic())
        return bucket.tokens

    def headrooms(self) -> dict[int, float]:
        """Headroom for every installation this process has actually called.

        Deliberately not every installation in the database: a bucket that has never
        seen a response has nothing to report but the default, and reporting a guess
        as a measurement is worse than reporting nothing.
        """
        return {
            installation_id: self.headroom(installation_id)
            for installation_id in list(self._buckets)
        }

    def retry_after(self, installation_id: int) -> float:
        return self.bucket(installation_id).wait_for_token(self._clock.monotonic())
