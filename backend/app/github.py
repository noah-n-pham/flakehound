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


def reset_token_cache() -> None:
    _tokens.clear()


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
