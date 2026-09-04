"""Being rate limited is not failing.

The queue has one way to put a row back (`mark_for_retry`) and it counts the
attempt the claim already incremented. Applied to a rate limit that is the wrong
answer twice over: the attempt was never spent, and five of them inside one
GitHub window would dead-letter history work with nothing wrong with it.

`defer` is the other way back: wait the time GitHub asked for, and keep every
attempt. Backfill is priority 1 precisely because history can wait.
"""

import httpx
import pytest
import respx
from sqlalchemy import select

from app.backfill import RUNS_JOB_TYPE, start_backfill
from app.config import get_settings
from app.github import reset_api_state, reset_token_cache
from app.models import EventQueue
from app.queue import claim_batch
from app.ratelimit import RateLimitExceeded
from tests.helpers import enqueue, one_session_factory
from tests.test_backfill import (
    FULL_NAME,
    INSTALLATION_ID,
    REPO_ID,
    TOKEN_URL,
    keypair,  # noqa: F401  # a fixture, used by name
    seed_repo,
)

RUNS_URL = f"https://api.github.com/repos/{FULL_NAME}/actions/runs"


async def row(session, queue_id: int) -> EventQueue:
    """Re-read from Postgres. Every queue write is a Core UPDATE, so an ORM
    instance the test inserted keeps the status it was born with."""
    session.expire_all()
    return await session.get(EventQueue, queue_id)


async def run_worker(session) -> int:
    from app.worker import run_once

    return await run_once(one_session_factory(session), batch_size=10)


# --------------------------------------------------------------------------- #
# The semantics, with the handler forced to raise
# --------------------------------------------------------------------------- #


@pytest.fixture
def rate_limited(monkeypatch):
    """Make every handler report that this installation is rate limited."""
    from app import worker

    async def limited(session, claimed):
        raise RateLimitExceeded(INSTALLATION_ID, 1800.0)

    monkeypatch.setattr(worker, "handle", limited)


async def test_a_rate_limited_row_goes_back_pending_and_waits(db_session, rate_limited):
    queued = enqueue(db_session, {"any": "payload"})
    await db_session.flush()

    assert await run_worker(db_session) == 1

    deferred = await row(db_session, queued.id)
    assert deferred.status == "pending"
    assert deferred.locked_at is None
    assert deferred.next_attempt_at is not None
    assert "rate limited" in deferred.last_error


async def test_the_attempt_is_given_back_because_none_was_spent(db_session, rate_limited):
    """The claim increments; being told to wait must undo that.

    Otherwise a row that meets a rate limit five times is indistinguishable from
    one whose handler is broken, and the queue dead-letters it.
    """
    queued = enqueue(db_session, {"any": "payload"})
    await db_session.flush()

    await run_worker(db_session)

    assert (await row(db_session, queued.id)).attempts == 0


async def test_the_row_is_not_claimable_until_the_wait_is_over(db_session, rate_limited):
    """`next_attempt_at` is the whole mechanism: without it the worker would
    re-claim the row on the next poll and hammer an API that just said no."""
    enqueue(db_session, {"any": "payload"})
    await db_session.flush()
    await run_worker(db_session)

    assert await claim_batch(db_session, 10) == []


async def test_a_row_rate_limited_more_often_than_its_ceiling_is_never_dead_lettered(
    db_session, rate_limited, monkeypatch
):
    """Six deferrals against a ceiling of five. This is the case `mark_for_retry`
    would have killed, and the reason `defer` exists at all."""
    queued = enqueue(db_session, {"any": "payload"})
    await db_session.flush()

    for _ in range(6):
        # Clear the wait each round, so the row is claimable again immediately.
        await db_session.execute(
            EventQueue.__table__.update()
            .where(EventQueue.id == queued.id)
            .values(next_attempt_at=None)
        )
        assert await run_worker(db_session) == 1

    final = await row(db_session, queued.id)
    assert final.status == "pending"
    assert final.attempts == 0
    assert final.completed_at is None


async def test_an_ordinary_failure_still_counts_its_attempt(db_session, monkeypatch):
    """The negative half. Without it, a worker that deferred *everything* would
    pass every test above."""
    from app import worker

    async def broken(session, claimed):
        raise ValueError("something is actually wrong")

    monkeypatch.setattr(worker, "handle", broken)
    queued = enqueue(db_session, {"any": "payload"})
    await db_session.flush()

    await run_worker(db_session)

    failed = await row(db_session, queued.id)
    assert failed.attempts == 1
    assert "ValueError" in failed.last_error


# --------------------------------------------------------------------------- #
# End to end: a real 403 from GitHub, through the real backfill handler
# --------------------------------------------------------------------------- #


@pytest.fixture
def github_over_budget(monkeypatch, keypair):  # noqa: F811  # the imported fixture
    monkeypatch.setenv("GITHUB_APP_ID", "4792446")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", keypair)
    monkeypatch.setenv("BACKFILL_DAYS", "7")
    monkeypatch.setenv("BACKFILL_WINDOW_DAYS", "3")
    get_settings.cache_clear()
    reset_token_cache()
    reset_api_state()
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
            )
        )
        yield router
    get_settings.cache_clear()
    reset_token_cache()
    reset_api_state()


async def test_a_backfill_page_that_meets_the_primary_limit_is_rescheduled(
    db_session, github_over_budget
):
    """The path this whole slice exists for, with nothing mocked but GitHub.

    A spent primary budget resets up to an hour out, which is far longer than a
    worker may hold a claimed row, so `api_request` refuses to wait and the
    queue takes the wait instead.
    """
    import time

    await seed_repo(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    github_over_budget.get(RUNS_URL).mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(time.time()) + 1800),
            },
        )
    )

    assert await run_worker(db_session) == 1

    queue_id = (
        await db_session.execute(
            select(EventQueue.id).where(EventQueue.job_type == RUNS_JOB_TYPE)
        )
    ).scalar_one()
    deferred = await row(db_session, queue_id)
    assert deferred.status == "pending"
    assert deferred.attempts == 0
    assert "rate limited" in deferred.last_error
    # And the crawl is untouched, waiting to resume from exactly where it was.
    repo = await seed_repo(db_session)
    assert repo.backfill_status == "running"
    assert repo.backfill_page == 1
