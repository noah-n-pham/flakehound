"""Crawling an observed repository through the existing backfill, unchanged.

The whole point of this slice was that nothing about the crawl is new machinery: the
same windowed walk, the same cursor on the repository row, the same handlers. Three
things do differ, and all three are asserted here — where the request's identity comes
from when there is no installation, that the work sits *below* a real user's history in
the queue, and that it walks the board's window rather than the full ninety days.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.backfill import (
    BACKFILL_PRIORITY,
    OBSERVED_PRIORITY,
    RUNS_JOB_TYPE,
    backfill_days,
    backfill_priority,
    handle_backfill_runs,
    request_identity,
    start_backfill,
    start_observed_backfills,
)
from app.config import get_settings
from app.models import EventQueue, Installation, Repository
from app.queue import claim_batch
from app.upserts import upsert_repository
from tests.conftest import INSTALLATION_ID
from tests.helpers import enqueue
from tests.test_ratelimit import headers

OBSERVED_ID = 699_532_645
OBSERVED_NAME = "astral-sh/uv"
INSTALLED_ID = 1_352_471_967
RUNS_URL = f"https://api.github.com/repos/{OBSERVED_NAME}/actions/runs"


@pytest.fixture
def observation_identity(monkeypatch):
    monkeypatch.setenv("OBSERVATION_INSTALLATION_ID", str(INSTALLATION_ID))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def observed_repo(session) -> Repository:
    await upsert_repository(
        session,
        installation_id=None,
        source="observed",
        repository={"id": OBSERVED_ID, "full_name": OBSERVED_NAME, "private": False},
    )
    await session.flush()
    return await session.get(Repository, OBSERVED_ID)


async def installed_repo(session) -> Repository:
    session.add(Installation(id=INSTALLATION_ID, account_login="noah-n-pham"))
    await session.flush()
    await upsert_repository(
        session,
        installation_id=INSTALLATION_ID,
        repository={"id": INSTALLED_ID, "full_name": "noah-n-pham/flakehound", "private": True},
    )
    await session.flush()
    return await session.get(Repository, INSTALLED_ID)


# --------------------------------------------------------------------------- #
# Whose token, and whose bucket
# --------------------------------------------------------------------------- #


async def test_an_observed_repo_borrows_the_observation_identity(
    db_session, observation_identity
):
    repo = await observed_repo(db_session)

    assert repo.installation_id is None
    assert request_identity(repo) == INSTALLATION_ID


async def test_an_installed_repo_uses_its_own_installation(db_session, observation_identity):
    """The borrowed identity is a fallback, never an override."""
    repo = await installed_repo(db_session)

    assert request_identity(repo) == INSTALLATION_ID
    assert repo.installation_id == INSTALLATION_ID


async def test_a_missing_observation_identity_raises_by_name(db_session, monkeypatch):
    """Anonymous requests would work at 60/hour instead of 5,000 — so the failure has
    to be loud rather than eighty times slower."""
    monkeypatch.delenv("OBSERVATION_INSTALLATION_ID", raising=False)
    get_settings.cache_clear()
    repo = await observed_repo(db_session)

    with pytest.raises(RuntimeError, match="OBSERVATION_INSTALLATION_ID"):
        request_identity(repo)

    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The crawl loses to everything else
# --------------------------------------------------------------------------- #


async def test_observed_work_is_queued_below_installed_backfill(
    db_session, observation_identity
):
    observed = await observed_repo(db_session)
    installed = await installed_repo(db_session)

    assert backfill_priority(installed) == BACKFILL_PRIORITY == 1
    assert backfill_priority(observed) == OBSERVED_PRIORITY == 2
    assert OBSERVED_PRIORITY > BACKFILL_PRIORITY


async def test_the_queue_serves_live_then_installed_then_observed(
    db_session, observation_identity
):
    """**The ordering that matters**, asserted through `claim_batch` rather than by
    reading the constants: nobody's dashboard may wait on the public board.

    Claimed one at a time, because `RETURNING` promises no ordering *within* a batch —
    the ordering is over which rows get selected, not the order they come back in. A
    batch of three would pass this test by accident or fail it by accident.
    """
    observed = await observed_repo(db_session)
    installed = await installed_repo(db_session)

    # Deliberately inserted worst-first, and all three share one transaction's
    # `created_at`, so neither age nor insertion order can be what selects them.
    await start_backfill(db_session, repo_id=observed.id)
    await db_session.flush()
    await start_backfill(db_session, repo_id=installed.id)
    await db_session.flush()
    enqueue(db_session, {"live": True}, priority=0)
    await db_session.flush()

    served = [(await claim_batch(db_session, 1))[0].payload for _ in range(3)]

    assert served == [
        {"live": True},
        {"repo_id": installed.id},
        {"repo_id": observed.id},
    ]


async def test_every_row_a_crawl_enqueues_keeps_the_low_priority(
    db_session, observation_identity, app_credentials, token_route
):
    """The first row is not the only one: a page enqueues the next page and a run's
    attempts, and any of those at priority 1 would let the crawl overtake real work."""
    repo = await observed_repo(db_session)
    await start_backfill(db_session, repo_id=repo.id)
    await db_session.flush()
    token_route.get(RUNS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 33_775_129_088,
                        "run_attempt": 2,
                        "head_sha": "c" * 40,
                        "workflow_id": 12,
                        "path": ".github/workflows/ci.yml",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ],
            },
            headers=headers(),
        )
    )

    await handle_backfill_runs(db_session, {"repo_id": repo.id})
    await db_session.flush()

    priorities = set(
        (
            await db_session.execute(
                select(EventQueue.priority).where(EventQueue.status == "pending")
            )
        ).scalars()
    )
    assert priorities == {OBSERVED_PRIORITY}


# --------------------------------------------------------------------------- #
# A shorter window than an installed repo gets
# --------------------------------------------------------------------------- #


async def test_an_observed_repo_walks_the_boards_window_not_ninety_days(
    db_session, observation_identity
):
    observed = await observed_repo(db_session)
    installed = await installed_repo(db_session)
    settings = get_settings()

    assert backfill_days(observed) == settings.observation_backfill_days == 30
    assert backfill_days(installed) == settings.backfill_days == 90


async def test_the_first_window_and_the_floor_agree_for_an_observed_repo(
    db_session, observation_identity, app_credentials, token_route
):
    """A floor derived from the wrong setting is invisible until the walk overshoots,
    so it is derived from the row in both places rather than stored twice."""
    repo = await observed_repo(db_session)
    await start_backfill(db_session, repo_id=repo.id)
    await db_session.flush()
    token_route.get(RUNS_URL).mock(
        return_value=httpx.Response(
            200, json={"total_count": 0, "workflow_runs": []}, headers=headers()
        )
    )
    floor = datetime.now(UTC).date() - timedelta(days=30 - 1)

    # Walk every window to the end and check none of them reaches past the floor.
    for _ in range(20):
        await handle_backfill_runs(db_session, {"repo_id": repo.id})
        await db_session.flush()
        if repo.backfill_status == "done":
            break
        assert repo.backfill_window_start >= floor

    assert repo.backfill_status == "done"


# --------------------------------------------------------------------------- #
# Starting the pool, bounded
# --------------------------------------------------------------------------- #


async def test_starting_the_crawl_is_bounded_by_its_limit(db_session, observation_identity):
    for offset in range(5):
        await upsert_repository(
            db_session,
            installation_id=None,
            source="observed",
            repository={
                "id": OBSERVED_ID + offset,
                "full_name": f"owner/repo-{offset}",
                "private": False,
            },
        )
    await db_session.flush()

    started = await start_observed_backfills(db_session, limit=2)

    assert len(started) == 2
    assert (
        await db_session.execute(
            select(EventQueue.id).where(EventQueue.job_type == RUNS_JOB_TYPE)
        )
    ).scalars().all() != []


async def test_starting_the_crawl_skips_repos_already_under_way(
    db_session, observation_identity
):
    """Run it twice and the second pass finds nothing: `pending` is the filter."""
    await observed_repo(db_session)
    await db_session.flush()

    first = await start_observed_backfills(db_session, limit=5)
    await db_session.flush()
    second = await start_observed_backfills(db_session, limit=5)

    assert first == [OBSERVED_ID]
    assert second == []


async def test_the_crawl_never_picks_up_an_installed_repo(db_session, observation_identity):
    """The installed backfill is started by installation events, not by this."""
    await installed_repo(db_session)
    await db_session.flush()

    assert await start_observed_backfills(db_session, limit=5) == []


async def test_the_crawl_never_picks_up_a_deactivated_repo(db_session, observation_identity):
    """A repo that went private or was archived is deactivated, not crawled again."""
    repo = await observed_repo(db_session)
    repo.active = False
    await db_session.flush()

    assert await start_observed_backfills(db_session, limit=5) == []
