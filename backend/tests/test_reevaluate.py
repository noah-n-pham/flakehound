"""Re-deriving detection from stored payloads (app/reevaluate.py).

The scenario throughout is the real one: deliveries were ingested and processed by a
worker whose detection did not yet know about the signal, so the facts are right and
the derived rows are missing. Deleting the flake events after delivery is how that
history is reproduced here.
"""

from sqlalchemy import delete, func, select

from app.models import EventQueue, FlakeEvent, Job, WorkflowRun
from app.queue import claim_batch
from app.reevaluate import enqueue_reevaluation
from app.worker import run_once
from tests import payloads
from tests.helpers import deliver, enqueue, one_session_factory
from tests.test_detection import RUN_ID, attempt, run_event


async def drain(session) -> int:
    """Run the worker until the queue has nothing pending left."""
    factory = one_session_factory(session)
    total = 0
    while (processed := await run_once(factory, batch_size=50)) > 0:
        total += processed
    return total


async def ingested_history(session) -> None:
    """A processed re-run recovery, with its derived rows removed again."""
    await deliver(
        session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure"),
        attempt(2, "success"),
    )
    await session.execute(delete(FlakeEvent))
    await session.flush()


async def count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_reevaluation_recovers_a_signal_that_did_not_exist_at_ingest(db_session):
    await ingested_history(db_session)
    assert await count(db_session, FlakeEvent) == 0

    queued = await enqueue_reevaluation(db_session)
    await drain(db_session)

    assert queued == 3
    signals = set((await db_session.execute(select(FlakeEvent.signal))).scalars())
    assert signals == {"rerun_recovery", "same_commit_disagreement"}


async def test_reevaluated_rows_are_backfill_priority_with_no_delivery_behind_them(db_session):
    await ingested_history(db_session)

    await enqueue_reevaluation(db_session)
    await db_session.flush()

    rows = (
        await db_session.execute(
            select(EventQueue).where(EventQueue.job_type == "reevaluate")
        )
    ).scalars()
    for row in rows:
        # Live events must keep overtaking history, and the delivery id is already
        # taken. It is the primary key of a row that exists.
        assert row.priority == 1
        assert row.delivery_id is None
        assert row.status == "pending"


async def test_a_live_event_is_claimed_before_reevaluated_history(db_session):
    await ingested_history(db_session)
    await enqueue_reevaluation(db_session)
    await db_session.flush()

    enqueue(db_session, attempt(3, "success"))
    await db_session.flush()

    claimed = await claim_batch(db_session, limit=1)

    assert len(claimed) == 1
    assert claimed[0].job_type == "webhook"


async def test_reevaluation_does_not_re_enqueue_its_own_output(db_session):
    """Otherwise every run would double the queue."""
    await ingested_history(db_session)

    first = await enqueue_reevaluation(db_session)
    await db_session.flush()
    second = await enqueue_reevaluation(db_session)
    await db_session.flush()

    assert first == second == 3
    total = (
        await db_session.execute(
            select(func.count())
            .select_from(EventQueue)
            .where(EventQueue.job_type == "reevaluate")
        )
    ).scalar_one()
    assert total == 6
    assert await count(db_session, EventQueue) == 9


async def test_reevaluation_changes_no_fact_row(db_session):
    """Idempotency layers 2 and 3 are what make re-processing safe rather than lossy."""
    await ingested_history(db_session)

    def snapshot(rows):
        return sorted(tuple(row) for row in rows)

    before_jobs = snapshot(
        (
            await db_session.execute(
                select(Job.id, Job.name, Job.conclusion, Job.head_sha, Job.created_at)
            )
        ).all()
    )
    before_runs = snapshot(
        (
            await db_session.execute(
                select(WorkflowRun.run_id, WorkflowRun.run_attempt, WorkflowRun.conclusion)
            )
        ).all()
    )

    await enqueue_reevaluation(db_session)
    await drain(db_session)

    after_jobs = snapshot(
        (
            await db_session.execute(
                select(Job.id, Job.name, Job.conclusion, Job.head_sha, Job.created_at)
            )
        ).all()
    )
    after_runs = snapshot(
        (
            await db_session.execute(
                select(WorkflowRun.run_id, WorkflowRun.run_attempt, WorkflowRun.conclusion)
            )
        ).all()
    )

    assert after_jobs == before_jobs
    assert after_runs == before_runs


async def test_reevaluation_can_be_limited_to_one_repo(db_session):
    await ingested_history(db_session)

    assert await enqueue_reevaluation(db_session, repo_id=payloads.REPO_ID) == 3
    assert await enqueue_reevaluation(db_session, repo_id=payloads.REPO_ID + 1) == 0
