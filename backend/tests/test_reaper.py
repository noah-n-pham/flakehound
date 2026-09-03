"""The stuck-row reaper: crash recovery.

A worker killed mid-message leaves a row claimed forever. A periodic sweep
returns rows that have been processing longer than a fixed timeout back to
pending. That timeout must exceed maximum processing time, or the reaper
duplicates work that is still running. It is also why every handler must be
idempotent: a reaped row will be processed twice."
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.config import get_settings
from app.handlers import handle
from app.models import EventQueue, Job
from app.queue import claim_batch, mark_done, reap_stuck
from app.worker import run_once, sweep
from tests import payloads
from tests.helpers import enqueue
from tests.helpers import one_session_factory as _one_session_factory


@pytest.fixture
def reaper_timeout(monkeypatch):
    """Pin the reaper timeout, so a test asserts against a number it chose."""

    def _set(seconds: int) -> None:
        monkeypatch.setenv("REAPER_TIMEOUT_SECONDS", str(seconds))
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


async def _rows(session) -> list[EventQueue]:
    session.expire_all()
    return list((await session.execute(select(EventQueue).order_by(EventQueue.id))).scalars())


async def _age_the_claim(session, seconds: int) -> None:
    """Backdate `locked_at`, which is how a claim becomes old without waiting."""
    await session.execute(
        update(EventQueue)
        .where(EventQueue.status == "processing")
        .values(locked_at=func.now() - timedelta(seconds=seconds))
    )


async def _strand_the_claim(session) -> None:
    """Age a claim past the timeout, whatever the timeout is configured to be."""
    await _age_the_claim(session, seconds=int(get_settings().reaper_timeout_seconds) + 60)


async def test_a_row_claimed_longer_than_the_timeout_returns_to_pending(db_session):
    enqueue(db_session, payloads.workflow_job())
    await db_session.flush()
    claimed = await claim_batch(db_session, limit=1)
    await _strand_the_claim(db_session)

    assert await reap_stuck(db_session) == [claimed[0].id]

    (row,) = await _rows(db_session)
    assert row.status == "pending"
    assert row.locked_at is None
    assert "reaped" in row.last_error
    # The attempt stays spent: a row that reliably kills its worker must run out
    # of attempts rather than be reaped forever.
    assert row.attempts == 1
    assert len(await claim_batch(db_session, limit=1)) == 1


async def test_a_claim_younger_than_the_timeout_survives_it(db_session, reaper_timeout):
    """The timeout exceeding maximum processing time is the whole safety margin.

    Reaping a row that is still being worked on is not a deadlock, it is
    duplicated work — so a claim younger than the timeout must survive however
    long it has been held. One four-minute-old claim is checked against two
    timeouts rather than one: an assertion that only ever says "not reaped"
    passes just as well against a reaper that reads no clock at all.

    Note also why the claim is backdated instead of merely fresh. Postgres'
    `now()` is the *transaction's* clock, so a claim taken in this transaction is
    exactly `now()` old and is younger than any timeout, including a broken one.
    """
    enqueue(db_session, payloads.workflow_job())
    await db_session.flush()
    await claim_batch(db_session, limit=1)
    await _age_the_claim(db_session, seconds=240)

    reaper_timeout(300)
    assert await reap_stuck(db_session) == []
    (row,) = await _rows(db_session)
    assert row.status == "processing"
    assert row.locked_at is not None

    reaper_timeout(120)
    assert len(await reap_stuck(db_session)) == 1
    (row,) = await _rows(db_session)
    assert row.status == "pending"


async def test_the_reaper_ignores_rows_it_does_not_own(db_session):
    """Only `processing` is ambiguous. Every other state means somebody decided."""
    done = enqueue(db_session, payloads.workflow_job())
    pending = enqueue(db_session, payloads.workflow_job())
    await db_session.flush()
    await mark_done(db_session, done.id)
    await db_session.execute(
        update(EventQueue)
        .where(EventQueue.id == pending.id)
        .values(locked_at=func.now() - timedelta(days=1))
    )

    assert await reap_stuck(db_session) == []

    statuses = {row.id: row.status for row in await _rows(db_session)}
    assert statuses == {done.id: "done", pending.id: "pending"}


async def test_a_killed_worker_loses_no_work(db_session):
    """The crash-recovery story end to end, at the point work is really lost.

    The claim commits before the handler runs, so a worker that dies mid-message
    leaves a claimed row and no facts. Nothing else will touch it until the
    reaper does.
    """
    enqueue(db_session, payloads.workflow_job())
    await db_session.flush()
    factory = _one_session_factory(db_session)

    # A worker claims the row and then dies: no handler, no mark_done.
    claimed = await claim_batch(db_session, limit=1)
    assert (await db_session.execute(select(func.count()).select_from(Job))).scalar_one() == 0
    # Claimed, so no other worker will pick it up either. The work is stranded.
    assert await run_once(factory, batch_size=10) == 0

    await _strand_the_claim(db_session)
    assert (await sweep(factory)).reaped == [claimed[0].id]

    assert await run_once(factory, batch_size=10) == 1

    job = (await db_session.execute(select(Job))).scalar_one()
    assert job.id == payloads.JOB_ID
    (row,) = await _rows(db_session)
    assert (row.status, row.attempts) == ("done", 2)


async def test_a_reaped_row_is_processed_twice_and_that_is_safe(db_session):
    """The worst case: the crash landed *after* the facts were committed.

    The handler had already written its rows and the process died before
    `mark_done`, so the reaper hands a second worker work that is genuinely
    finished. SPEC accepts this and requires the handlers absorb it.
    """
    enqueue(db_session, payloads.workflow_job())
    await db_session.flush()
    factory = _one_session_factory(db_session)

    claimed = await claim_batch(db_session, limit=1)
    await handle(db_session, claimed[0])  # facts written...
    await db_session.commit()  # ...and committed, then the worker dies.

    first = (await db_session.execute(select(Job))).scalar_one()
    before = (first.id, first.name, first.conclusion, first.completed_at, first.created_at)

    await _strand_the_claim(db_session)
    await sweep(factory)
    assert await run_once(factory, batch_size=10) == 1

    db_session.expire_all()
    second = (await db_session.execute(select(Job))).scalar_one()
    assert (
        second.id,
        second.name,
        second.conclusion,
        second.completed_at,
        second.created_at,
    ) == before
    assert (await db_session.execute(select(func.count()).select_from(Job))).scalar_one() == 1


async def test_one_sweep_reaps_then_dead_letters_a_row_on_its_last_attempt(db_session):
    """Order inside the sweep is load-bearing.

    A row whose final attempt died with its worker comes back pending with its
    attempts spent, which nothing will ever claim. Failing spent rows after
    reaping — not before — is what makes it visible in the same pass.
    """
    enqueue(db_session, payloads.workflow_job(), attempts=2, max_attempts=3)
    await db_session.flush()
    claimed = await claim_batch(db_session, limit=1)
    assert claimed[0].exhausted
    await _strand_the_claim(db_session)

    result = await sweep(_one_session_factory(db_session))

    assert result.reaped == [claimed[0].id]
    assert result.failed == [claimed[0].id]
    (row,) = await _rows(db_session)
    assert (row.status, row.attempts) == ("failed", 3)
    assert row.completed_at is not None
