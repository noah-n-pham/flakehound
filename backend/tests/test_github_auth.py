"""App JWT → installation token, and the cache in front of it.

The RSA key here is generated per test session and never leaves memory. The real
one lives outside the repository and is never read by the suite.
External HTTP is respx, never live.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import get_settings
from app.github import (
    GitHubAuthError,
    app_jwt,
    installation_token,
    reset_token_cache,
)

APP_ID = 4_792_446
INSTALLATION_ID = 158_221_992
TOKEN_URL = f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"


@pytest.fixture(scope="session")
def keypair() -> tuple[str, object]:
    """A throwaway RSA key, so a test can verify a signature it did not make."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


@pytest.fixture
def app_credentials(monkeypatch, keypair):
    pem, _ = keypair
    monkeypatch.setenv("GITHUB_APP_ID", str(APP_ID))
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    get_settings.cache_clear()
    reset_token_cache()
    yield
    get_settings.cache_clear()
    reset_token_cache()


def _token_response(token: str = "ghs_exampletoken", minutes: int = 60) -> httpx.Response:
    expires_at = datetime.now(UTC) + timedelta(minutes=minutes)
    return httpx.Response(
        201,
        json={
            "token": token,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "permissions": {"actions": "read", "metadata": "read"},
            "repository_selection": "selected",
        },
    )


# --------------------------------------------------------------------------- #
# The assertion we sign
# --------------------------------------------------------------------------- #


def test_the_app_jwt_is_signed_and_carries_the_claims_github_requires(app_credentials, keypair):
    _, public_key = keypair

    claims = jwt.decode(app_jwt(), public_key, algorithms=["RS256"], issuer=str(APP_ID))

    assert claims["iss"] == str(APP_ID)
    # Backdated by a real margin, not merely "before now": a fast clock here
    # makes GitHub reject the assertion as issued in the future, and `iat = now`
    # would satisfy a naive comparison a microsecond later.
    assert claims["iat"] <= datetime.now(UTC).timestamp() - 30
    # GitHub's hard ceiling is ten minutes and it refuses anything longer.
    assert 0 < claims["exp"] - claims["iat"] <= 600


def test_the_app_jwt_uses_rs256_not_a_symmetric_algorithm(app_credentials):
    assert jwt.get_unverified_header(app_jwt())["alg"] == "RS256"


def test_a_missing_credential_is_an_error_at_use_not_at_import(monkeypatch):
    """An ingest-only deploy has no key and must still start."""
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(GitHubAuthError, match="GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY"):
            app_jwt()
    finally:
        get_settings.cache_clear()


def test_a_private_key_with_escaped_newlines_still_loads(monkeypatch, keypair):
    """How a PEM arrives when it has been through an env var or a JSON secret."""
    pem, public_key = keypair
    monkeypatch.setenv("GITHUB_APP_ID", str(APP_ID))
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem.replace("\n", "\\n"))
    get_settings.cache_clear()
    try:
        claims = jwt.decode(app_jwt(), public_key, algorithms=["RS256"], issuer=str(APP_ID))
        assert claims["iss"] == str(APP_ID)
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The exchange and its cache
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_jwt_is_exchanged_for_an_installation_token(app_credentials, keypair):
    _, public_key = keypair
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())

    assert await installation_token(INSTALLATION_ID) == "ghs_exampletoken"

    request = route.calls.last.request
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    # The App JWT is the credential here, not an installation token, which is
    # what this call exists to obtain.
    scheme, _, presented = request.headers["authorization"].partition(" ")
    assert scheme == "Bearer"
    assert jwt.decode(presented, public_key, algorithms=["RS256"], issuer=str(APP_ID))


@respx.mock
async def test_a_cached_token_is_reused_without_a_second_request(app_credentials):
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())

    first = await installation_token(INSTALLATION_ID)
    second = await installation_token(INSTALLATION_ID)

    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_a_token_near_expiry_is_renewed(app_credentials):
    """"Until near expiry" is the criterion: a token with four minutes left is
    not worth handing to a backfill that will still be running in five."""
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            _token_response("ghs_nearlyexpired", minutes=4),
            _token_response("ghs_fresh", minutes=60),
        ]
    )

    assert await installation_token(INSTALLATION_ID) == "ghs_nearlyexpired"
    assert await installation_token(INSTALLATION_ID) == "ghs_fresh"
    assert route.call_count == 2
    # And the replacement is then cached like any other.
    assert await installation_token(INSTALLATION_ID) == "ghs_fresh"
    assert route.call_count == 2


@respx.mock
async def test_each_installation_gets_its_own_token(app_credentials):
    other_id = 2
    mine = respx.post(TOKEN_URL).mock(return_value=_token_response("ghs_mine"))
    theirs = respx.post(
        f"https://api.github.com/app/installations/{other_id}/access_tokens"
    ).mock(return_value=_token_response("ghs_theirs"))

    assert await installation_token(INSTALLATION_ID) == "ghs_mine"
    assert await installation_token(other_id) == "ghs_theirs"
    # Neither call invalidated the other's entry.
    assert await installation_token(INSTALLATION_ID) == "ghs_mine"
    assert (mine.call_count, theirs.call_count) == (1, 1)


@respx.mock
async def test_concurrent_callers_buy_one_token_between_them(app_credentials):
    """A backfill fans out, and a request per page would spend the rate limit on
    tokens. The cache is checked again inside the lock for this reason.

    The mocked exchange has to actually suspend, or this test proves nothing: an
    instant response lets the first caller populate the cache before the others
    are ever scheduled, so they hit the outer check and no stampede forms. A
    real exchange is a network round trip, and that is what this reproduces.
    """

    async def slow_exchange(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return _token_response()

    route = respx.post(TOKEN_URL).mock(side_effect=slow_exchange)

    tokens = await asyncio.gather(*(installation_token(INSTALLATION_ID) for _ in range(8)))

    assert set(tokens) == {"ghs_exampletoken"}
    assert route.call_count == 1


@respx.mock
async def test_a_rejected_key_raises_and_caches_nothing(app_credentials):
    """401 means the key or the app id is wrong. Retrying cannot fix that, but a
    cached failure would outlive the fix."""
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(401, json={"message": "A JSON web token could not be decoded"}),
            _token_response("ghs_afterthefix"),
        ]
    )

    with pytest.raises(GitHubAuthError, match="401"):
        await installation_token(INSTALLATION_ID)

    assert await installation_token(INSTALLATION_ID) == "ghs_afterthefix"
    assert route.call_count == 2


@respx.mock
async def test_a_server_error_is_left_for_the_queue_to_retry(app_credentials):
    """Not a GitHubAuthError: a 500 is transient, and the queue's retry with
    backoff is the right response to it."""
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(500, text="upstream"))

    with pytest.raises(httpx.HTTPStatusError):
        await installation_token(INSTALLATION_ID)
