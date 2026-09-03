"""Retry, backoff, and the `failed` terminal state.

The rule: a raised exception returns the row to pending with the error recorded, so it
retries. Past a small attempt ceiling it stops being selected and is effectively
dead-lettered; a sweep marks those failed so they are visible rather than
silently stuck."
"""

from datetime import UTC, datetime

from sqlalchemy import select, update

from app.config import get_settings
from app.models import EventQueue, Job
from app.queue import claim_batch, fail_exhausted, retry_delay_seconds
from app.worker import run_once, sweep
from tests import payloads
from tests.helpers import enqueue
from tests.helpers import one_session_factory as _one_session_factory

# No repository and no installation, so the handler raises every single time.
POISON = {"workflow_job": {"id": 1}}


async def _row(session) -> EventQueue:
    """The queue row as Postgres holds it.

    `expire_all` is not decoration: the queue's writes are Core UPDATEs with
    `synchronize_session=False`, so an instance the session already loaded keeps
    the status it was inserted with and a test would assert against a stale copy.
    """
    session.expire_all()
    return (await session.execute(select(EventQueue))).scalar_one()


async def _clear_backoff(session) -> None:
    """Pretend the wait has elapsed, so a test does not have to sleep through it."""
    await session.execute(update(EventQueue).values(next_attempt_at=None))


async def test_a_failed_attempt_waits_before_being_retried(db_session):
    enqueue(db_session, POISON)
    await db_session.flush()

    await run_once(_one_session_factory(db_session), batch_size=10)

    row = await _row(db_session)
    assert (row.status, row.attempts) == ("pending", 1)
    assert row.next_attempt_at > datetime.now(UTC)
    assert "ValueError" in row.last_error
    # Still pending, but not yet due: the next poll must step over it rather than
    # spending a second attempt microseconds after the first.
    assert await claim_batch(db_session, limit=10) == []

    await _clear_backoff(db_session)
    assert len(await claim_batch(db_session, limit=10)) == 1


async def test_the_backoff_doubles_and_is_capped():
    base = get_settings().retry_backoff_seconds
    cap = get_settings().retry_backoff_max_seconds

    assert retry_delay_seconds(1) == base
    assert retry_delay_seconds(2) == base * 2
    assert retry_delay_seconds(3) == base * 4
    assert retry_delay_seconds(99) == cap
    # The default ceiling of five attempts must span minutes, not one poll cycle.
    assert sum(retry_delay_seconds(n) for n in (1, 2, 3, 4)) >= 60


async def test_the_last_attempt_dead_letters_the_row_rather_than_leaving_it_pending(db_session):
    """A row at its ceiling would never be claimed again, so it must say so."""
    enqueue(db_session, POISON, max_attempts=2)
    await db_session.flush()
    factory = _one_session_factory(db_session)

    await run_once(factory, batch_size=10)
    assert (await _row(db_session)).status == "pending"

    await _clear_backoff(db_session)
    await run_once(factory, batch_size=10)

    row = await _row(db_session)
    assert (row.status, row.attempts) == ("failed", 2)
    assert row.completed_at is not None
    assert row.locked_at is None
    assert "ValueError" in row.last_error
    # Terminal in both directions: never claimed again, and no facts were written.
    assert await claim_batch(db_session, limit=10) == []
    assert (await db_session.execute(select(Job))).all() == []


async def test_a_retry_that_succeeds_completes_normally(db_session):
    """Backoff must not turn a transient failure into a lost delivery."""
    enqueue(db_session, POISON)
    await db_session.flush()
    factory = _one_session_factory(db_session)

    await run_once(factory, batch_size=10)
    assert (await _row(db_session)).status == "pending"

    # The transient condition clears: the same row, now with a payload the
    # handler can process.
    await db_session.execute(update(EventQueue).values(payload=payloads.workflow_job()))
    await _clear_backoff(db_session)
    await run_once(factory, batch_size=10)

    row = await _row(db_session)
    assert (row.status, row.attempts) == ("done", 2)
    assert row.completed_at is not None
    assert (await db_session.execute(select(Job))).one()


async def test_the_sweep_fails_rows_that_are_out_of_attempts(db_session):
    """The case the retry path cannot cover: a row returned to pending, spent.

    A worker killed between the claim and recording the outcome leaves its row
    for the reaper, which returns it to pending with the attempt already
    counted. Nothing will claim it again, and pending is a lie.
    """
    spent = enqueue(db_session, POISON, attempts=5, max_attempts=5)
    live = enqueue(db_session, POISON, attempts=4, max_attempts=5)
    await db_session.flush()

    assert await fail_exhausted(db_session) == [spent.id]

    await db_session.refresh(spent)
    await db_session.refresh(live)
    assert spent.status == "failed"
    assert spent.completed_at is not None
    assert live.status == "pending"


async def test_the_sweep_leaves_a_claimed_row_alone(db_session):
    """A processing row belongs to the reaper, even when its attempts are spent."""
    enqueue(db_session, POISON, attempts=4, max_attempts=5)
    await db_session.flush()
    await claim_batch(db_session, limit=1)

    assert await fail_exhausted(db_session) == []
    assert (await _row(db_session)).status == "processing"


async def test_the_worker_sweeps(db_session):
    spent = enqueue(db_session, POISON, attempts=5, max_attempts=5)
    await db_session.flush()

    assert (await sweep(_one_session_factory(db_session))).failed == [spent.id]
    assert (await _row(db_session)).status == "failed"
