"""Dequeue semantics and the workflow_job path (SPEC §5, §6)."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import EventQueue, Installation, Job, Repository, WorkflowRun
from app.queue import claim_batch, mark_done, mark_for_retry
from app.worker import run_once
from tests import payloads
from tests.helpers import enqueue
from tests.helpers import one_session_factory as _one_session_factory


async def test_claiming_marks_rows_processing_and_counts_the_attempt(db_session):
    enqueue(db_session, payloads.workflow_job())
    await db_session.flush()

    claimed = await claim_batch(db_session, limit=10)

    assert len(claimed) == 1
    assert claimed[0].event == "workflow_job"
    assert claimed[0].attempts == 1

    row = (await db_session.execute(select(EventQueue))).scalar_one()
    assert row.status == "processing"
    assert row.locked_at is not None


async def test_live_events_are_claimed_before_backfill(db_session):
    enqueue(db_session, {"backfill": 1}, event=None, priority=1)
    await db_session.flush()
    enqueue(db_session, payloads.workflow_job(), priority=0)
    await db_session.flush()

    claimed = await claim_batch(db_session, limit=1)

    assert len(claimed) == 1
    assert claimed[0].event == "workflow_job"


async def test_a_row_out_of_attempts_is_no_longer_claimed(db_session):
    enqueue(db_session, payloads.workflow_job(), attempts=5)
    await db_session.flush()

    assert await claim_batch(db_session, limit=10) == []


async def test_a_second_worker_skips_a_locked_row(database_url):
    """SKIP LOCKED is the whole trick: never blocked, never the same row twice.

    Two real connections, because a lock is only observable across sessions.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as setup:
            enqueue(setup, payloads.workflow_job())
            await setup.commit()

        async with maker() as worker_a, maker() as worker_b:
            claimed_by_a = await claim_batch(worker_a, limit=10)
            # A holds the row lock: its transaction is still open.
            claimed_by_b = await claim_batch(worker_b, limit=10)

            assert len(claimed_by_a) == 1
            assert claimed_by_b == []

            await worker_a.rollback()
            await worker_b.rollback()
    finally:
        async with maker() as cleanup:
            await cleanup.execute(EventQueue.__table__.delete())
            await cleanup.commit()
        await engine.dispose()


async def test_a_workflow_job_event_produces_a_run_and_a_job(db_session):
    """The last hop of the walking skeleton: queue row in, job row out."""
    enqueue(db_session, payloads.workflow_job())
    await db_session.flush()

    sessionmaker = _one_session_factory(db_session)
    processed = await run_once(sessionmaker, batch_size=10)

    assert processed == 1

    job = (await db_session.execute(select(Job))).scalar_one()
    assert job.id == payloads.JOB_ID
    assert job.name == "test (ubuntu-latest, 3.12)"
    assert job.conclusion == "success"
    assert job.head_sha == payloads.SHA
    assert job.runner_labels == ["ubuntu-latest"]
    assert (job.step_count, job.completed_step_count) == (3, 3)

    run = (await db_session.execute(select(WorkflowRun))).scalar_one()
    assert (run.run_id, run.run_attempt) == (payloads.RUN_ID, 1)
    assert run.head_sha == payloads.SHA
    # A job payload carries no workflow id, so the stub leaves it unknown.
    assert run.workflow_id is None

    repo = (await db_session.execute(select(Repository))).scalar_one()
    assert repo.full_name == "khoi/flakehound"
    assert repo.private is False

    installation = (await db_session.execute(select(Installation))).scalar_one()
    assert installation.account_login == "khoi"

    queue_row = (await db_session.execute(select(EventQueue))).scalar_one()
    assert queue_row.status == "done"
    assert queue_row.completed_at is not None


async def test_a_rerun_is_stored_as_a_second_attempt(db_session):
    """Signal A depends on both attempts surviving as distinct rows."""
    enqueue(db_session, payloads.workflow_job(job_id=1, run_attempt=1, conclusion="failure"))
    enqueue(db_session, payloads.workflow_job(job_id=2, run_attempt=2, conclusion="success"))
    await db_session.flush()

    await run_once(_one_session_factory(db_session), batch_size=10)

    attempts = (
        await db_session.execute(
            select(Job.run_attempt, Job.conclusion).order_by(Job.run_attempt)
        )
    ).all()
    assert attempts == [(1, "failure"), (2, "success")]

    runs = (await db_session.execute(select(func.count()).select_from(WorkflowRun))).scalar_one()
    assert runs == 2


async def test_processing_the_same_payload_twice_changes_nothing(db_session):
    """Idempotency layer 2, in miniature. The 100x replay test lands in Section C."""
    enqueue(db_session, payloads.workflow_job())
    await db_session.flush()
    sessionmaker = _one_session_factory(db_session)
    await run_once(sessionmaker, batch_size=10)

    first = (await db_session.execute(select(Job))).scalar_one()
    first_seen = (first.id, first.name, first.conclusion, first.created_at)

    enqueue(db_session, payloads.workflow_job())
    await db_session.flush()
    await run_once(sessionmaker, batch_size=10)

    second = (await db_session.execute(select(Job))).scalar_one()
    assert (second.id, second.name, second.conclusion, second.created_at) == first_seen
    assert (await db_session.execute(select(func.count()).select_from(Job))).scalar_one() == 1


async def test_a_failing_handler_returns_the_row_to_pending_with_its_error(db_session):
    enqueue(db_session, {"workflow_job": {"id": 1}})  # no repository, no installation
    await db_session.flush()

    await run_once(_one_session_factory(db_session), batch_size=10)

    row = (await db_session.execute(select(EventQueue))).scalar_one()
    assert row.status == "pending"
    assert row.attempts == 1
    assert "ValueError" in row.last_error
    assert (await db_session.execute(select(func.count()).select_from(Job))).scalar_one() == 0


async def test_marking_done_and_marking_for_retry(db_session):
    row = enqueue(db_session, payloads.workflow_job())
    await db_session.flush()

    await mark_for_retry(db_session, row.id, "boom")
    await db_session.refresh(row)
    assert (row.status, row.last_error) == ("pending", "boom")

    await mark_done(db_session, row.id)
    await db_session.refresh(row)
    assert row.status == "done"
    assert row.locked_at is None
