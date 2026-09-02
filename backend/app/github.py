"""Authenticating as the GitHub App, then as one of its installations (SPEC §7).

Two credentials, and the distinction matters. The **App JWT** is signed locally
with the App's private key, proves only "I am this App", and can do nothing but
ask about installations. An **installation token** is what GitHub gives back in
exchange, and it is what reads a repository's runs and jobs. It lasts an hour and
is scoped to that one installation.

So every API call needs a token, tokens expire, and buying one costs a request.
The cache is an in-process dict, which is enough because there is exactly one
worker (SPEC §12 rejects Redis for this).
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from app.config import Settings, get_settings
from app.logging import get_logger
from app.ratelimit import RateLimiter, RateLimitExceeded

log = get_logger(__name__)

# GitHub caps the App JWT at ten minutes. Nine leaves room for clock skew on
# their side without ever presenting an expired assertion.
JWT_TTL = timedelta(minutes=9)
# GitHub backdates nothing, so a fast clock here makes the JWT "issued in the
# future" and every request 401s. A minute of backdating is the documented fix.
JWT_SKEW = timedelta(minutes=1)
# An installation token lives an hour. Renewing it with five minutes left keeps a
# long backfill from carrying a token that expires mid-page.
RENEW_BEFORE_EXPIRY = timedelta(minutes=5)

API_VERSION = "2022-11-28"

# A rate-limited response is retried, because the retry waits out the block the
# response itself declared rather than hammering. Three is enough to ride out a
# secondary limit; a primary limit will exceed the maximum wait long before this.
RATE_LIMIT_ATTEMPTS = 3


class GitHubAuthError(RuntimeError):
    """The App could not authenticate. Never raised for a transient failure."""


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime

    def usable_at(self, now: datetime) -> bool:
        return now + RENEW_BEFORE_EXPIRY < self.expires_at


_tokens: dict[int, InstallationToken] = {}
# One lock, not one per installation: a token is bought once an hour per install,
# so contention is theoretical, and a single lock cannot deadlock or leak keys.
_lock = asyncio.Lock()

# The limiter has to outlive a request to be worth anything — its whole state is
# what the last response said — so it is module-level beside the token cache.
_limiter: RateLimiter | None = None
_client: httpx.AsyncClient | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(
            max_wait_seconds=get_settings().github_rate_limit_max_wait_seconds
        )
    return _limiter


def get_api_client() -> httpx.AsyncClient:
    """One pooled client for the API, unlike the token exchange's throwaway.

    A backfill is hundreds of sequential requests to one host, so the TLS
    handshake is worth doing once.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


def reset_token_cache() -> None:
    _tokens.clear()


def reset_api_state() -> None:
    """Drop the limiter and the pooled client. For tests."""
    global _limiter, _client
    _limiter = None
    _client = None


def _credentials(settings: Settings) -> tuple[int, str]:
    """Fail here rather than at import, so an ingest-only deploy still starts."""
    if not settings.github_app_id or not settings.github_app_private_key:
        missing = [
            name
            for name, value in (
                ("GITHUB_APP_ID", settings.github_app_id),
                ("GITHUB_APP_PRIVATE_KEY", settings.github_app_private_key),
            )
            if not value
        ]
        raise GitHubAuthError(
            f"cannot authenticate as the GitHub App: {' and '.join(missing)} is not set"
        )
    return settings.github_app_id, settings.github_app_private_key


def app_jwt(*, now: datetime | None = None) -> str:
    """A short-lived RS256 assertion that we are the App. Signed locally."""
    settings = get_settings()
    app_id, private_key = _credentials(settings)
    now = now or datetime.now(UTC)
    return jwt.encode(
        {
            "iat": int((now - JWT_SKEW).timestamp()),
            "exp": int((now + JWT_TTL).timestamp()),
            "iss": str(app_id),
        },
        private_key,
        algorithm="RS256",
    )


async def fetch_installation_token(installation_id: int) -> InstallationToken:
    """Trade the App JWT for a token scoped to one installation.

    A short-lived client per call on purpose: this happens once an hour per
    installation, so pooling buys nothing, and the shared client the rest of the
    API will use belongs with the rate limiter rather than here.
    """
    settings = get_settings()
    url = f"{settings.github_api_base_url}/app/installations/{installation_id}/access_tokens"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )

    if response.status_code in (401, 403, 404):
        # Wrong key, wrong app id, or the install is gone. Retrying cannot help.
        raise GitHubAuthError(
            f"installation {installation_id} token exchange failed: "
            f"{response.status_code} {response.text[:200]}"
        )
    response.raise_for_status()

    body = response.json()
    expires_at = datetime.fromisoformat(body["expires_at"])
    log.info(
        "github.token_issued",
        installation_id=installation_id,
        expires_at=expires_at.isoformat(),
        repository_selection=body.get("repository_selection"),
    )
    return InstallationToken(token=body["token"], expires_at=expires_at)


async def installation_token(installation_id: int) -> str:
    """The cached token for an installation, bought or renewed as needed.

    The cache is checked twice, once outside the lock and once inside: the first
    check keeps the common path lock-free, and the second stops a burst of
    concurrent callers from each buying a token after queueing on the same lock.
    """
    now = datetime.now(UTC)
    cached = _tokens.get(installation_id)
    if cached and cached.usable_at(now):
        return cached.token

    async with _lock:
        cached = _tokens.get(installation_id)
        if cached and cached.usable_at(datetime.now(UTC)):
            return cached.token
        fresh = await fetch_installation_token(installation_id)
        _tokens[installation_id] = fresh
        return fresh.token


def _is_rate_limited(response: httpx.Response) -> bool:
    """Tell a rate limit apart from an ordinary refusal.

    GitHub answers both a spent budget and a missing permission with 403, and
    the difference is entirely in the headers: the primary limit says
    `x-ratelimit-remaining: 0`, the secondary one sends `retry-after`. A 403 that
    says neither is a permissions problem and retrying it is pointless.
    """
    if response.status_code not in (403, 429):
        return False
    headers = response.headers
    if "retry-after" in headers:
        return True
    remaining = headers.get("x-ratelimit-remaining")
    return remaining is not None and remaining.strip() == "0"


async def api_request(
    installation_id: int,
    method: str,
    path: str,
    *,
    params: dict[str, object] | None = None,
    limiter: RateLimiter | None = None,
) -> httpx.Response:
    """One GitHub API call as this installation, paced by its own token bucket.

    Every read of a repository's runs and jobs goes through here, so the limiter
    sees every response and nothing can spend the budget behind its back. A
    rate-limited response is retried, and the retry is what waits: `observe` has
    already written the block into the bucket, so the next `acquire` sleeps
    exactly as long as GitHub asked for — or refuses, if that is longer than a
    claimed queue row may be held.
    """
    settings = get_settings()
    limiter = limiter or get_limiter()
    client = get_api_client()
    url = path if path.startswith("http") else f"{settings.github_api_base_url}{path}"

    for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
        token = await installation_token(installation_id)
        await limiter.acquire(installation_id)
        response = await client.request(
            method,
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        limiter.observe(installation_id, response.headers)
        if not _is_rate_limited(response):
            return response
        log.warning(
            "github.rate_limited",
            installation_id=installation_id,
            status=response.status_code,
            attempt=attempt,
            retry_after=response.headers.get("retry-after"),
            reset=response.headers.get("x-ratelimit-reset"),
        )

    raise RateLimitExceeded(installation_id, limiter.retry_after(installation_id))
