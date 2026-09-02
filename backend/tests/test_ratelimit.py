"""The per-installation token bucket (SPEC §7).

Time is faked rather than waited on, which is the only way these assertions can
be exact: a test that really slept would have to assert a range and would take
an hour to exercise a window turnover. The fake clock records every sleep, so
"how long did it wait" is a first-class assertion here rather than a side effect.
"""

import asyncio

import pytest

from app.ratelimit import Clock, RateLimiter, RateLimitExceeded

INSTALLATION_ID = 158_221_992
OTHER_INSTALLATION_ID = 2
EPOCH = 1_800_000_000.0


class FakeClock(Clock):
    """Advances only when something sleeps, so elapsed time is what was asked for."""

    def __init__(self) -> None:
        self._monotonic = 1_000.0
        self._wall = EPOCH
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._monotonic

    def wall(self) -> float:
        return self._wall

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._wall += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def limiter_for(clock: FakeClock, *, limit: int = 5, window: float = 5.0) -> RateLimiter:
    """A five-per-five-seconds budget: the same shape as 5000/hour, compressed.

    `window_seconds` exists for exactly this. Testing pacing against the real
    3600-second window would mean asserting waits of twenty minutes.
    """
    return RateLimiter(
        clock=clock, default_limit=limit, window_seconds=window, max_wait_seconds=60.0
    )


def headers(**kwargs: object) -> dict[str, str]:
    """Response headers in GitHub's spelling."""
    names = {
        "limit": "x-ratelimit-limit",
        "remaining": "x-ratelimit-remaining",
        "reset": "x-ratelimit-reset",
        "retry_after": "retry-after",
    }
    return {names[key]: str(value) for key, value in kwargs.items()}


# --------------------------------------------------------------------------- #
# Pacing
# --------------------------------------------------------------------------- #


async def test_a_bucket_with_budget_left_does_not_wait(clock):
    limiter = limiter_for(clock)

    for _ in range(5):
        await limiter.acquire(INSTALLATION_ID)

    assert clock.sleeps == []
    assert limiter.headroom(INSTALLATION_ID) == pytest.approx(0.0)


async def test_an_exhausted_bucket_paces_at_the_refill_rate(clock):
    """Five per five seconds is one per second, and that is what the sixth waits."""
    limiter = limiter_for(clock)
    for _ in range(5):
        await limiter.acquire(INSTALLATION_ID)

    await limiter.acquire(INSTALLATION_ID)
    await limiter.acquire(INSTALLATION_ID)

    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


async def test_each_installation_has_its_own_budget(clock):
    """One repo's backfill must not throttle another account's."""
    limiter = limiter_for(clock)
    for _ in range(5):
        await limiter.acquire(INSTALLATION_ID)

    await limiter.acquire(OTHER_INSTALLATION_ID)

    assert clock.sleeps == []


# --------------------------------------------------------------------------- #
# What the headers say wins
# --------------------------------------------------------------------------- #


async def test_githubs_count_lowers_ours_and_never_raises_it(clock):
    """Our count is a guess between responses; GitHub's is the fact.

    Both directions matter. Being told 2 when we thought 5 must cost us three
    tokens, and being told 4000 when we have spent down to 1 must not hand them
    back — the difference is requests in flight, which we have spent and GitHub
    has not yet counted.
    """
    limiter = limiter_for(clock)

    limiter.observe(INSTALLATION_ID, headers(remaining=2))
    assert limiter.headroom(INSTALLATION_ID) == pytest.approx(2.0)

    limiter.observe(INSTALLATION_ID, headers(remaining=4_000))
    assert limiter.headroom(INSTALLATION_ID) == pytest.approx(2.0)


async def test_the_limit_header_replaces_the_assumed_budget(clock):
    """5,000 is a default, not a constant: it scales with installation size."""
    limiter = limiter_for(clock)

    limiter.observe(INSTALLATION_ID, headers(limit=15_000, remaining=15_000))

    bucket = limiter.bucket(INSTALLATION_ID)
    assert bucket.capacity == 15_000.0
    assert bucket.refill_per_second == pytest.approx(15_000 / 5.0)


async def test_a_spent_budget_waits_for_the_reset_and_the_window_turns_over(clock):
    """GitHub's window does not slide, it turns over wholesale, so the wait is
    until the reset and what comes back is the entire allowance.

    The reset is deliberately **sooner than a full refill would take**: at one
    token per second, two seconds of continuous refill buys two tokens and a
    turnover buys five. Any longer and the two models agree and the assertion
    stops testing the turnover at all.
    """
    limiter = limiter_for(clock)
    limiter.observe(INSTALLATION_ID, headers(remaining=0, reset=int(EPOCH + 2)))

    await limiter.acquire(INSTALLATION_ID)

    assert clock.sleeps == [pytest.approx(2.0)]
    assert limiter.headroom(INSTALLATION_ID) == pytest.approx(4.0)


async def test_retry_after_blocks_even_with_budget_to_spare(clock):
    """The secondary limit is about request shape, not budget. It arrives with
    thousands remaining and a bucket that only watched `remaining` would ignore it."""
    limiter = limiter_for(clock)
    limiter.observe(INSTALLATION_ID, headers(remaining=4, retry_after=20))

    await limiter.acquire(INSTALLATION_ID)

    assert clock.sleeps == [pytest.approx(20.0)]


async def test_a_reset_already_in_the_past_costs_no_wait(clock):
    """A slow response, or a clock a few seconds behind GitHub's."""
    limiter = limiter_for(clock)
    limiter.observe(INSTALLATION_ID, headers(remaining=0, reset=int(EPOCH - 5)))

    await limiter.acquire(INSTALLATION_ID)

    assert clock.sleeps == []


# --------------------------------------------------------------------------- #
# The reaper's guarantee
# --------------------------------------------------------------------------- #


async def test_a_wait_longer_than_the_worker_will_hold_a_row_is_refused(clock):
    """A claimed row held past `reaper_timeout_seconds` is given to another
    worker. Sleeping out a primary limit can take the best part of an hour, so
    the wait goes back to the queue, which already retries with backoff."""
    limiter = RateLimiter(
        clock=clock, default_limit=5, window_seconds=5.0, max_wait_seconds=60.0
    )
    limiter.observe(INSTALLATION_ID, headers(remaining=0, reset=int(EPOCH + 1_800)))

    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.acquire(INSTALLATION_ID)

    assert raised.value.installation_id == INSTALLATION_ID
    assert raised.value.retry_after == pytest.approx(1_800.0)
    # It refused rather than sleeping: no time passed at all.
    assert clock.sleeps == []


async def test_a_wait_inside_the_ceiling_is_slept_not_refused(clock):
    """The positive half of the assertion above. Without it, a limiter that
    refused everything would pass."""
    limiter = RateLimiter(
        clock=clock, default_limit=5, window_seconds=5.0, max_wait_seconds=60.0
    )
    limiter.observe(INSTALLATION_ID, headers(remaining=0, reset=int(EPOCH + 59)))

    await limiter.acquire(INSTALLATION_ID)

    assert clock.sleeps == [pytest.approx(59.0)]


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


class CountingClock(Clock):
    """Real time, counted. The fake clock cannot be used for this one.

    A clock that only advances when something sleeps makes every waiter move
    global time forward on its own, so concurrent waiters are indistinguishable
    from sequential ones and the test passes against any implementation. This
    sleeps for real — a tenth of a second at a time — and counts the waits.
    """

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(seconds)


async def test_waiters_queue_rather_than_stampede():
    """Three callers on an empty bucket must wait one behind the other.

    The lock is held across the sleep for this reason. Released before it, all
    three wake together, one takes the single token that appeared and the other
    two go back to sleep — the wait count grows quadratically in the number of
    waiters, which at backfill fan-out is the difference between three wakeups
    and dozens. Correctness is not at stake: the check and the take have no
    `await` between them, so nobody over-issues either way. Only the herd is.
    """
    clock = CountingClock()
    # A token every tenth of a second, so this costs ~0.3s of real time.
    limiter = RateLimiter(clock=clock, default_limit=5, window_seconds=0.5)
    for _ in range(5):
        await limiter.acquire(INSTALLATION_ID)

    await asyncio.gather(*(limiter.acquire(INSTALLATION_ID) for _ in range(3)))

    assert len(clock.sleeps) == 3
    assert limiter.headroom(INSTALLATION_ID) < 1.0
