"""`api_request` — every GitHub API call, paced by the installation's bucket.

The limiter is unit-tested against a fake clock in `test_ratelimit.py`. What is
tested here is the wiring: that responses reach the bucket, that a rate-limited
response is retried after the pause it asked for, and that an ordinary 403 is
not mistaken for one.
"""

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import get_settings
from app.github import api_request, get_limiter, reset_api_state, reset_token_cache
from app.ratelimit import RateLimitExceeded
from tests.test_ratelimit import EPOCH, FakeClock, headers, limiter_for

APP_ID = 4_792_446
INSTALLATION_ID = 158_221_992
TOKEN_URL = f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
RUNS_URL = "https://api.github.com/repos/noah-n-pham/flakehound/actions/runs"
RUNS_PATH = "/repos/noah-n-pham/flakehound/actions/runs"


@pytest.fixture
def app_credentials(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("GITHUB_APP_ID", str(APP_ID))
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    get_settings.cache_clear()
    reset_token_cache()
    reset_api_state()
    yield
    get_settings.cache_clear()
    reset_token_cache()
    reset_api_state()


@pytest.fixture
def token_route():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                201,
                json={"token": "ghs_installation", "expires_at": "2099-01-01T00:00:00Z"},
            )
        )
        yield router


def ok(payload: object = None, **limits: object) -> httpx.Response:
    return httpx.Response(200, json=payload or {"total_count": 0}, headers=headers(**limits))


def limited(status: int = 403, **limits: object) -> httpx.Response:
    return httpx.Response(
        status, json={"message": "API rate limit exceeded"}, headers=headers(**limits)
    )


# --------------------------------------------------------------------------- #
# The wiring
# --------------------------------------------------------------------------- #


async def test_the_call_carries_the_installation_token_not_the_app_jwt(
    app_credentials, token_route
):
    route = token_route.get(RUNS_URL).mock(return_value=ok(limit=5_000, remaining=4_999))

    response = await api_request(
        INSTALLATION_ID, "GET", RUNS_PATH, limiter=limiter_for(FakeClock())
    )

    assert response.status_code == 200
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer ghs_installation"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"


async def test_query_parameters_reach_github(app_credentials, token_route):
    route = token_route.get(RUNS_URL).mock(return_value=ok())

    await api_request(
        INSTALLATION_ID,
        "GET",
        RUNS_PATH,
        params={"per_page": 100, "created": "2026-06-01..2026-06-30"},
        limiter=limiter_for(FakeClock()),
    )

    url = route.calls.last.request.url
    assert url.params["per_page"] == "100"
    assert url.params["created"] == "2026-06-01..2026-06-30"


async def test_every_response_teaches_the_bucket(app_credentials, token_route):
    """Nothing may spend the budget behind the limiter's back, which is the whole
    reason there is one entry point rather than a client passed around."""
    clock = FakeClock()
    limiter = limiter_for(clock, limit=5_000, window=3_600.0)
    token_route.get(RUNS_URL).mock(
        return_value=ok(limit=5_000, remaining=4_321, reset=int(EPOCH + 1_200))
    )

    await api_request(INSTALLATION_ID, "GET", RUNS_PATH, limiter=limiter)

    assert limiter.headroom(INSTALLATION_ID) == pytest.approx(4_321.0)


async def test_the_default_limiter_is_shared_between_calls(app_credentials, token_route):
    """State that did not survive the request would be no state at all."""
    token_route.get(RUNS_URL).mock(return_value=ok(limit=5_000, remaining=4_000))

    await api_request(INSTALLATION_ID, "GET", RUNS_PATH)

    assert get_limiter().headroom(INSTALLATION_ID) == pytest.approx(4_000.0, abs=1.0)


# --------------------------------------------------------------------------- #
# Being told to wait
# --------------------------------------------------------------------------- #


async def test_a_secondary_limit_is_retried_after_the_pause_it_asked_for(
    app_credentials, token_route
):
    clock = FakeClock()
    route = token_route.get(RUNS_URL).mock(
        side_effect=[limited(403, remaining=4_998, retry_after=17), ok(remaining=4_997)]
    )

    response = await api_request(
        INSTALLATION_ID, "GET", RUNS_PATH, limiter=limiter_for(clock, limit=5_000, window=3_600.0)
    )

    assert response.status_code == 200
    assert route.call_count == 2
    # It waited exactly what the header said, rather than a backoff of its own.
    assert clock.sleeps == [pytest.approx(17.0)]


async def test_a_spent_primary_budget_is_retried_after_the_window_resets(
    app_credentials, token_route
):
    clock = FakeClock()
    route = token_route.get(RUNS_URL).mock(
        side_effect=[
            limited(429, limit=5_000, remaining=0, reset=int(EPOCH + 45)),
            ok(limit=5_000, remaining=4_999, reset=int(EPOCH + 3_600)),
        ]
    )

    response = await api_request(
        INSTALLATION_ID, "GET", RUNS_PATH, limiter=limiter_for(clock, limit=5_000, window=3_600.0)
    )

    assert response.status_code == 200
    assert clock.sleeps == [pytest.approx(45.0)]
    assert route.call_count == 2


async def test_a_limit_longer_than_the_ceiling_goes_back_to_the_queue(
    app_credentials, token_route
):
    """Half an hour is longer than a worker may hold a claimed row, so this
    raises instead of sleeping and the queue's retry with backoff owns the wait."""
    clock = FakeClock()
    token_route.get(RUNS_URL).mock(
        return_value=limited(403, limit=5_000, remaining=0, reset=int(EPOCH + 1_800))
    )

    with pytest.raises(RateLimitExceeded):
        await api_request(
            INSTALLATION_ID,
            "GET",
            RUNS_PATH,
            limiter=limiter_for(clock, limit=5_000, window=3_600.0),
        )

    assert clock.sleeps == []


async def test_a_limit_that_never_lifts_stops_after_three_attempts(
    app_credentials, token_route
):
    clock = FakeClock()
    route = token_route.get(RUNS_URL).mock(
        return_value=limited(403, remaining=4_990, retry_after=5)
    )

    with pytest.raises(RateLimitExceeded):
        await api_request(
            INSTALLATION_ID,
            "GET",
            RUNS_PATH,
            limiter=limiter_for(clock, limit=5_000, window=3_600.0),
        )

    assert route.call_count == 3
    assert clock.sleeps == [pytest.approx(5.0), pytest.approx(5.0)]


async def test_a_plain_403_is_a_permissions_error_and_is_returned_not_retried(
    app_credentials, token_route
):
    """GitHub answers a spent budget and a missing permission with the same
    status. Only the headers separate them, and retrying the second is pointless."""
    route = token_route.get(RUNS_URL).mock(
        return_value=httpx.Response(
            403,
            json={"message": "Resource not accessible by integration"},
            headers=headers(limit=5_000, remaining=4_872),
        )
    )

    response = await api_request(
        INSTALLATION_ID, "GET", RUNS_PATH, limiter=limiter_for(FakeClock())
    )

    assert response.status_code == 403
    assert route.call_count == 1


async def test_a_404_is_returned_for_the_caller_to_decide_about(
    app_credentials, token_route
):
    """A known edge case: a re-run after 30+ days 404s from the jobs
    endpoint. That is data, not a failure, and the backfill has to see it."""
    token_route.get(RUNS_URL).mock(
        return_value=httpx.Response(404, json={"message": "Not Found"}, headers=headers())
    )

    response = await api_request(
        INSTALLATION_ID, "GET", RUNS_PATH, limiter=limiter_for(FakeClock())
    )

    assert response.status_code == 404
