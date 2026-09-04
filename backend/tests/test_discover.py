"""Candidate discovery, and the direction `source` is allowed to move in.

Two things here are load-bearing. Search must not share the core rate limiter, because
one search response would clamp the core bucket from 5,000 to 30 and throttle the
installed backfill. And an observed write must never demote an installed repository,
because otherwise the webhook and the crawl fight over every repo that is both.
"""

from datetime import UTC, datetime

import httpx
import pytest

from app.discover import (
    BANDS,
    Band,
    band_query,
    discover,
    get_search_limiter,
    reset_search_limiter,
    search_band,
)
from app.github import get_limiter, reset_api_state
from app.models import Repository
from app.observe import MIN_COMPLETED_RUNS
from app.upserts import upsert_repository
from tests.conftest import INSTALLATION_ID
from tests.test_ratelimit import headers

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
SEARCH_URL = "https://api.github.com/search/repositories"
ONE_BAND = (Band(name="test", low=100, high=299),)


@pytest.fixture(autouse=True)
def fresh_limiters():
    reset_search_limiter()
    reset_api_state()
    yield
    reset_search_limiter()
    reset_api_state()


def stub_candidate(router, full_name: str, repo_id: int, *, runs: int = 250, **overrides):
    """The three admission responses for one candidate."""
    url = f"https://api.github.com/repos/{full_name}"
    payload = {
        "id": repo_id,
        "full_name": full_name,
        "private": False,
        "archived": False,
        "fork": False,
        "is_template": False,
        "pushed_at": "2026-09-03T09:12:41Z",
        "default_branch": "main",
        "stargazers_count": 150,
        **overrides,
    }
    router.get(url).mock(return_value=httpx.Response(200, json=payload, headers=headers()))
    router.get(f"{url}/actions/workflows").mock(
        return_value=httpx.Response(
            200, json={"workflows": [{"id": 1, "state": "active"}]}, headers=headers()
        )
    )
    router.get(f"{url}/actions/runs").mock(
        return_value=httpx.Response(
            200,
            json={"total_count": runs, "workflow_runs": [{"id": 9, "event": "push"}]},
            headers=headers(),
        )
    )


def stub_search(router, *names: str, limit: int = 30, remaining: int = 29):
    router.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": len(names),
                "items": [{"full_name": name} for name in names],
            },
            headers=headers(limit=limit, remaining=remaining),
        )
    )


# --------------------------------------------------------------------------- #
# The bands
# --------------------------------------------------------------------------- #


def test_the_bands_are_the_three_the_brief_asked_for_and_stop_at_500():
    """No huge famous repositories: the pool is deliberately capped at 500 stars."""
    assert [(band.low, band.high) for band in BANDS] == [(5, 99), (100, 299), (300, 500)]
    assert max(band.high for band in BANDS) == 500


def test_the_query_mirrors_the_criteria_and_orders_by_activity():
    query = band_query(BANDS[1], now=NOW)

    assert "stars:100..299" in query
    assert "pushed:>=2026-08-04" in query
    assert "is:public" in query and "archived:false" in query and "fork:false" in query
    # Nothing about outcomes appears in the query, because search must not pre-select
    # for flakiness — that is what would make the board report its own ordering.
    assert "conclusion" not in query and "failure" not in query


async def test_search_orders_by_updated_not_by_stars(app_credentials, token_route):
    stub_search(token_route, "a/one")

    await search_band(BANDS[0], installation_id=INSTALLATION_ID, wanted=1, now=NOW)

    params = token_route.calls.last.request.url.params
    assert params["sort"] == "updated"
    assert params["order"] == "desc"


async def test_search_stops_once_it_has_enough(app_credentials, token_route):
    route = token_route.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"full_name": f"a/{i}"} for i in range(5)]},
            headers=headers(),
        )
    )

    found, skipped = await search_band(
        BANDS[0], installation_id=INSTALLATION_ID, wanted=5, now=NOW
    )

    assert len(found) == 5
    assert skipped == 0
    assert route.call_count == 1


async def test_search_stops_on_an_empty_page_rather_than_paging_forever(
    app_credentials, token_route
):
    route = token_route.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"items": []}, headers=headers())
    )

    found, skipped = await search_band(
        BANDS[0], installation_id=INSTALLATION_ID, wanted=30, now=NOW
    )
    assert (found, skipped) == ([], 0)
    assert route.call_count == 1


# --------------------------------------------------------------------------- #
# Search's budget is not core's
# --------------------------------------------------------------------------- #


def test_the_search_limiter_is_thirty_a_minute_not_five_thousand_an_hour():
    limiter = get_search_limiter()

    assert limiter.bucket(INSTALLATION_ID).capacity == 30.0
    # 30 per 60s, not 5000 per 3600s.
    assert limiter.bucket(INSTALLATION_ID).refill_per_second == pytest.approx(0.5)


async def test_a_search_response_cannot_shrink_the_core_budget(app_credentials, token_route):
    """**The bug this separation exists to prevent.**

    `RateLimiter.observe()` believes `x-ratelimit-limit`. Search answers 30 and core
    answers 5,000, so one search routed through the core limiter would leave the
    installed backfill pacing itself at 30 requests an hour.
    """
    core = get_limiter()
    core.observe(INSTALLATION_ID, {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4999"})
    assert core.bucket(INSTALLATION_ID).capacity == 5000.0
    stub_search(token_route, "a/one", limit=30, remaining=29)

    await search_band(BANDS[0], installation_id=INSTALLATION_ID, wanted=1, now=NOW)

    assert core.bucket(INSTALLATION_ID).capacity == 5000.0
    assert get_search_limiter().bucket(INSTALLATION_ID).capacity == 30.0


# --------------------------------------------------------------------------- #
# Discovery end to end
# --------------------------------------------------------------------------- #


async def test_an_admitted_candidate_becomes_an_observed_row(
    app_credentials, token_route, db_session
):
    stub_search(token_route, "astral-sh/uv")
    stub_candidate(token_route, "astral-sh/uv", 699_532_645)

    result = await discover(
        db_session, installation_id=INSTALLATION_ID, bands=ONE_BAND, per_band=1, now=NOW
    )

    assert result.admitted == ["astral-sh/uv"]
    stored = await db_session.get(Repository, 699_532_645)
    assert (stored.source, stored.installation_id, stored.private) == ("observed", None, False)
    assert (stored.full_name, stored.owner, stored.name) == ("astral-sh/uv", "astral-sh", "uv")
    assert stored.active is True


async def test_a_rejected_candidate_is_counted_and_not_stored(
    app_credentials, token_route, db_session
):
    stub_search(token_route, "someone/quiet")
    stub_candidate(token_route, "someone/quiet", 111, runs=MIN_COMPLETED_RUNS - 1)

    result = await discover(
        db_session, installation_id=INSTALLATION_ID, bands=ONE_BAND, per_band=1, now=NOW
    )

    assert result.admitted == []
    assert result.rejections == {"too_little_history": 1}
    assert await db_session.get(Repository, 111) is None


async def test_discovery_is_idempotent(app_credentials, token_route, db_session):
    """A second pass over the same pool must converge, not duplicate or flip anything."""
    stub_search(token_route, "astral-sh/uv")
    stub_candidate(token_route, "astral-sh/uv", 699_532_645)

    first = await discover(
        db_session, installation_id=INSTALLATION_ID, bands=ONE_BAND, per_band=1, now=NOW
    )
    later = await discover(
        db_session, installation_id=INSTALLATION_ID, bands=ONE_BAND, per_band=1, now=NOW
    )

    stored = await db_session.get(Repository, 699_532_645)
    assert (stored.source, stored.installation_id) == ("observed", None)
    assert first.screened == 1 and first.skipped == 0
    assert later.screened == 0 and later.skipped == 1


async def test_the_rejection_counts_are_the_audit_trail(
    app_credentials, token_route, db_session
):
    """"We looked at three and admitted one" is the honest answer to how these
    repositories were chosen, so the counts are returned rather than logged and lost."""
    stub_search(token_route, "a/good", "a/archived", "a/quiet")
    stub_candidate(token_route, "a/good", 1)
    stub_candidate(token_route, "a/archived", 2, archived=True)
    stub_candidate(token_route, "a/quiet", 3, runs=0)

    result = await discover(
        db_session, installation_id=INSTALLATION_ID, bands=ONE_BAND, per_band=3, now=NOW
    )

    assert result.screened == 3
    assert result.skipped == 0
    assert result.admitted == ["a/good"]
    assert result.rejections == {"archived": 1, "too_little_history": 1}
    assert result.bands[0].requests_spent == 9


# --------------------------------------------------------------------------- #
# Already-stored repositories are not re-screened
# --------------------------------------------------------------------------- #


async def _store_observed(session, repo_id: int, full_name: str) -> None:
    await upsert_repository(
        session,
        installation_id=None,
        source="observed",
        repository={"id": repo_id, "full_name": full_name, "private": False},
    )
    await session.flush()


async def test_a_stored_repo_is_skipped_and_does_not_spend_screening_requests(
    app_credentials, token_route, db_session
):
    """The budget this exists to save: three core requests we already spent."""
    await _store_observed(db_session, 10, "old/one")
    stub_search(token_route, "old/one", "new/two")
    stub_candidate(token_route, "new/two", 20)

    result = await discover(
        db_session, installation_id=INSTALLATION_ID, bands=ONE_BAND, per_band=1, now=NOW
    )

    assert result.skipped == 1
    assert result.screened == 1
    assert result.admitted == ["new/two"]
    assert await db_session.get(Repository, 10) is not None
    # respx is assert_all_mocked: a screen of old/one would have no route and fail.


async def test_an_installed_repo_is_skipped_the_same_way(
    app_credentials, token_route, db_session
):
    from app.models import Installation

    db_session.add(Installation(id=INSTALLATION_ID, account_login="khoi"))
    await db_session.flush()
    await upsert_repository(
        db_session,
        installation_id=INSTALLATION_ID,
        repository={"id": 43, "full_name": "khoi/thing", "private": False},
    )
    await db_session.flush()
    stub_search(token_route, "khoi/thing", "new/two")
    stub_candidate(token_route, "new/two", 20)

    result = await discover(
        db_session, installation_id=INSTALLATION_ID, bands=ONE_BAND, per_band=1, now=NOW
    )

    assert result.skipped == 1
    assert result.admitted == ["new/two"]
    stored = await db_session.get(Repository, 43)
    assert (stored.source, stored.installation_id) == ("installed", INSTALLATION_ID)


async def test_skip_is_by_github_id_even_when_the_full_name_changed(
    app_credentials, token_route, db_session
):
    await _store_observed(db_session, 10, "old/name")
    token_route.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": 10, "full_name": "renamed/name"},
                    {"id": 20, "full_name": "new/two"},
                ]
            },
            headers=headers(),
        )
    )
    stub_candidate(token_route, "new/two", 20)

    result = await discover(
        db_session, installation_id=INSTALLATION_ID, bands=ONE_BAND, per_band=1, now=NOW
    )

    assert result.skipped == 1
    assert result.admitted == ["new/two"]


async def test_search_pages_past_stored_hits_to_fill_the_quota(
    app_credentials, token_route, db_session
):
    await _store_observed(db_session, 1, "old/one")

    def _by_page(request):
        page = request.url.params.get("page", "1")
        # A full page of already-stored hits: short pages mean "no more", so
        # paging past the pool only happens when search still has more.
        items = (
            [{"id": 1, "full_name": "old/one"}] * 100
            if page == "1"
            else [{"id": 2, "full_name": "new/two"}]
        )
        return httpx.Response(200, json={"items": items}, headers=headers())

    token_route.get(SEARCH_URL).mock(side_effect=_by_page)
    stub_candidate(token_route, "new/two", 2)

    result = await discover(
        db_session, installation_id=INSTALLATION_ID, bands=ONE_BAND, per_band=1, now=NOW
    )

    assert result.skipped == 100
    assert result.admitted == ["new/two"]
    search_pages = [
        call.request.url.params.get("page")
        for call in token_route.calls
        if str(call.request.url).startswith(SEARCH_URL)
    ]
    assert search_pages == ["1", "2"]


# --------------------------------------------------------------------------- #
# `source` moves one way
# --------------------------------------------------------------------------- #


async def test_installing_the_app_claims_an_observed_row_in_place(db_session):
    """SPEC §4's "becomes installed in place, under the same GitHub repo id".

    This is the path a real webhook takes for a repo the crawl already found, and it
    must keep the row rather than conflict with it.
    """
    from app.models import Installation

    db_session.add(Installation(id=INSTALLATION_ID, account_login="astral-sh"))
    await upsert_repository(
        db_session,
        installation_id=None,
        source="observed",
        repository={"id": 42, "full_name": "astral-sh/uv", "private": False},
    )
    await db_session.flush()

    await upsert_repository(
        db_session,
        installation_id=INSTALLATION_ID,
        repository={"id": 42, "full_name": "astral-sh/uv", "private": False},
    )
    await db_session.flush()

    stored = await db_session.get(Repository, 42)
    assert (stored.source, stored.installation_id) == ("installed", INSTALLATION_ID)


async def test_crawling_an_installed_repo_cannot_demote_it(db_session):
    """The asymmetry. Without it, a crawl would strip the installation off a real
    customer's repo and the check constraint would start rejecting webhook writes."""
    from app.models import Installation

    db_session.add(Installation(id=INSTALLATION_ID, account_login="khoi"))
    # Autoflush is off in this session, so the installation has to reach the database
    # before a row can reference it.
    await db_session.flush()
    await upsert_repository(
        db_session,
        installation_id=INSTALLATION_ID,
        repository={"id": 43, "full_name": "khoi/thing", "private": False},
    )
    await db_session.flush()

    await upsert_repository(
        db_session,
        installation_id=None,
        source="observed",
        repository={"id": 43, "full_name": "khoi/thing", "private": False},
    )
    await db_session.flush()

    stored = await db_session.get(Repository, 43)
    assert (stored.source, stored.installation_id) == ("installed", INSTALLATION_ID)


async def test_an_observed_write_refuses_a_private_repo(db_session):
    """The check constraint restated in Python, so the error names the caller."""
    with pytest.raises(ValueError, match="must be public"):
        await upsert_repository(
            db_session,
            installation_id=None,
            source="observed",
            repository={"id": 44, "full_name": "khoi/secret", "private": True},
        )


async def test_an_observed_write_refuses_an_installation(db_session):
    with pytest.raises(ValueError, match="no installation"):
        await upsert_repository(
            db_session,
            installation_id=INSTALLATION_ID,
            source="observed",
            repository={"id": 45, "full_name": "khoi/thing", "private": False},
        )


async def test_an_installed_write_refuses_a_missing_installation(db_session):
    with pytest.raises(ValueError, match="needs an installation"):
        await upsert_repository(
            db_session,
            installation_id=None,
            repository={"id": 46, "full_name": "khoi/thing", "private": False},
        )
