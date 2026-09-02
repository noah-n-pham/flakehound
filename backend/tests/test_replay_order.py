"""Fact writes must not depend on the order their deliveries are processed in (SPEC §6).

GitHub sends three deliveries per job and per run — queued, in progress, completed — and
nothing guarantees the order they are *processed* in. `claim_batch` chooses which rows to
claim by priority and age, but `RETURNING` carries no ordering guarantee within a batch,
and the reaper re-runs abandoned messages beside live ones.

This is a regression suite for a bug that reached production: `upsert_job` wrote `status`
and `conclusion` outright, so an `in_progress` body applied after the `completed` one
blanked a real conclusion, and the job silently stopped being an opportunity (turn 19).
"""

from itertools import permutations

from sqlalchemy import select

from app.models import FlakeEvent, Job, WorkflowRun
from app.rollup import rollup_repository
from app.stats import flaky_jobs
from tests import payloads
from tests.helpers import deliver
from tests.test_detection import (
    OTHER_WORKFLOW_ID,
    RUN_ID,
    SHA,
    WORKFLOW_ID,
    attempt,
    run_event,
)

JOB_ID = 99_996_168_477
JOB_NAME = "build and deploy"


def job_lifecycle() -> tuple[dict, dict, dict]:
    """The three deliveries GitHub sends for one job execution, in their true order."""
    common = {"job_id": JOB_ID, "run_id": RUN_ID, "name": JOB_NAME}
    return (
        payloads.workflow_job(
            **common, run_attempt=1, status="queued", conclusion=None, completed_steps=0
        ),
        payloads.workflow_job(
            **common,
            run_attempt=1,
            status="in_progress",
            conclusion=None,
            completed_steps=4,
            total_steps=14,
        ),
        payloads.workflow_job(
            **common, run_attempt=1, status="completed", conclusion="success", completed_steps=14
        ),
    )


async def stored_job(session) -> Job:
    return (await session.execute(select(Job))).scalar_one()


async def test_an_in_progress_delivery_cannot_erase_a_conclusion(db_session):
    """The production bug, reduced to two deliveries in the order that broke it."""
    _, in_progress, completed = job_lifecycle()

    await deliver(db_session, completed, in_progress)

    job = await stored_job(db_session)
    assert job.conclusion == "success"
    assert job.status == "completed"
    assert (job.step_count, job.completed_step_count) == (14, 14)
    assert job.completed_at is not None


async def test_a_queued_delivery_cannot_erase_a_conclusion(db_session):
    queued, _, completed = job_lifecycle()

    await deliver(db_session, completed, queued)

    job = await stored_job(db_session)
    assert (job.status, job.conclusion) == ("completed", "success")
    assert job.completed_step_count == 14


async def test_every_processing_order_converges_on_the_same_job_row(db_session):
    """The property the spec actually asks for: replay in any order, same state."""
    fields = (
        Job.status,
        Job.conclusion,
        Job.started_at,
        Job.completed_at,
        Job.step_count,
        Job.completed_step_count,
        Job.runner_name,
    )
    results = []
    for order in permutations(job_lifecycle()):
        await db_session.execute(Job.__table__.delete())
        await db_session.flush()
        await deliver(db_session, *order)
        row = (await db_session.execute(select(*fields))).one()
        results.append(tuple(row))

    assert len(set(results)) == 1, results
    status, conclusion, started, completed, steps, done_steps, runner = results[0]
    assert (status, conclusion, steps, done_steps) == ("completed", "success", 14, 14)
    assert started is not None and completed is not None and runner is not None


def run_lifecycle() -> tuple[dict, dict, dict]:
    """The three `workflow_run` deliveries one push produces."""

    def event(status: str, conclusion: str | None) -> dict:
        return payloads.workflow_run(
            run_id=RUN_ID, head_sha=SHA, status=status, conclusion=conclusion
        )

    return event("queued", None), event("in_progress", None), event("completed", "success")


async def test_a_run_advances_to_completed_instead_of_sticking_at_its_first_status(db_session):
    """The mirror wart: COALESCE froze a run's status at whichever delivery landed first."""
    await deliver(db_session, *run_lifecycle())

    run = (await db_session.execute(select(WorkflowRun))).scalar_one()
    assert (run.status, run.conclusion) == ("completed", "success")


async def test_a_late_run_delivery_cannot_undo_a_finished_run(db_session):
    queued, in_progress, completed = run_lifecycle()

    await deliver(db_session, completed, in_progress, queued)

    run = (await db_session.execute(select(WorkflowRun))).scalar_one()
    assert (run.status, run.conclusion) == ("completed", "success")


async def test_every_processing_order_converges_on_the_same_run_row(db_session):
    fields = (
        WorkflowRun.status,
        WorkflowRun.conclusion,
        WorkflowRun.event,
        WorkflowRun.workflow_id,
    )
    results = []
    for order in permutations(run_lifecycle()):
        await db_session.execute(WorkflowRun.__table__.delete())
        await db_session.flush()
        await deliver(db_session, *order)
        results.append(tuple((await db_session.execute(select(*fields))).one()))

    assert len(set(results)) == 1, results
    assert results[0][:2] == ("completed", "success")


async def test_a_runs_workflow_id_is_first_write_wins(db_session):
    """A run's workflow cannot change, so a stored value is never replaced.

    GitHub will not disagree with itself about this, and that is the point: an immutable
    identity should be order-independent by construction rather than by trusting upstream.
    Last-write-wins left it depending on which delivery was processed last.
    """
    await deliver(
        db_session,
        payloads.workflow_run(run_id=RUN_ID, head_sha=SHA, workflow_id=WORKFLOW_ID),
        payloads.workflow_run(run_id=RUN_ID, head_sha=SHA, workflow_id=OTHER_WORKFLOW_ID),
    )

    run = (await db_session.execute(select(WorkflowRun))).scalar_one()
    assert run.workflow_id == WORKFLOW_ID


async def test_detection_survives_a_late_in_progress_delivery(db_session):
    """The symptom that made this visible: a blanked conclusion left Signal A short.

    Attempt 3 is delivered complete, then its `in_progress` body arrives late. Before the
    fix that erased the success, the recovery vanished from the opportunities, and the
    leaderboard read four opportunities where there were five.
    """
    late = payloads.workflow_job(
        job_id=99_998_597_127,
        run_id=RUN_ID,
        run_attempt=3,
        name=JOB_NAME,
        status="in_progress",
        conclusion=None,
        completed_steps=4,
        total_steps=18,
    )
    await deliver(
        db_session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure", job_id=99_996_168_477, name=JOB_NAME),
        attempt(2, "failure", job_id=99_997_527_370, name=JOB_NAME),
        attempt(3, "success", job_id=99_998_597_127, name=JOB_NAME, completed_steps=18),
        late,
    )

    await rollup_repository(db_session, repo_id=payloads.REPO_ID)
    board = await flaky_jobs(db_session, repo_id=payloads.REPO_ID)
    job = next(row for row in board if row.job_name == JOB_NAME)

    assert (job.opportunities, job.failures, job.flakes) == (3, 2, 3)
    signals = set((await db_session.execute(select(FlakeEvent.signal))).scalars())
    assert signals == {"rerun_recovery", "same_commit_disagreement"}
