"""Flake detection — SPEC §2, which is binding.

Signal A (re-run recovery) compares one job's conclusions across the attempts of a
single run. GitHub's re-run creates a new attempt under the same run id and the
commit SHA cannot change, which is what makes it the cleanest signal available:
the code provably did not change between the two conclusions.

Signal B (same-commit disagreement) compares them across every run on one commit,
which catches flakiness that shows up between separate runs — a push and a pull
request on the same SHA, or a manual re-dispatch — rather than between attempts.
A SHA is content-addressed, so a force-push cannot corrupt the grouping: identical
SHA means identical tree.

The eligibility rules from that section's edge-case table live in `job_outcome`,
because a signal reasons over *opportunities* rather than over raw conclusions. A
cancelled or skipped job says nothing about flakiness, and neither does a runner
that died before completing a single step.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.logging import get_logger
from app.models import FlakeEvent, Job

log = get_logger(__name__)

RERUN_RECOVERY = "rerun_recovery"
SAME_COMMIT_DISAGREEMENT = "same_commit_disagreement"

SUCCESS = "success"
FAILURE = "failure"


@dataclass(frozen=True)
class Opportunity:
    """A terminal job run eligible for evaluation (SPEC §2 definitions)."""

    job_id: int
    run_id: int
    run_attempt: int
    outcome: str
    workflow_id: int | None
    head_sha: str
    completed_at: datetime | None


def job_outcome(
    conclusion: str | None,
    *,
    step_count: int | None = None,
    completed_step_count: int | None = None,
) -> str | None:
    """Reduce GitHub's conclusion to `success`, `failure`, or not-an-opportunity.

    Every branch here is a row of SPEC §2's edge-case table:

    * `cancelled`, `skipped`, and the advisory conclusions — excluded;
    * `null` — still running, excluded until the completion event re-evaluates it;
    * `timed_out` — a config flag, treated as an eligible failure by default;
    * zero completed steps out of a known number of them is a dead runner rather
      than a flaky test, and is excluded by default under its own flag.
    """
    settings = get_settings()
    if conclusion == SUCCESS:
        return SUCCESS
    if conclusion == FAILURE or (conclusion == "timed_out" and settings.timed_out_is_failure):
        if settings.exclude_infra_failures and _is_infra_failure(step_count, completed_step_count):
            return None
        return FAILURE
    return None


def _is_infra_failure(step_count: int | None, completed_step_count: int | None) -> bool:
    """The runner died: steps were planned and none of them finished.

    An unrecorded count is not evidence of anything, so it is not an infra failure.
    """
    return step_count is not None and completed_step_count == 0


async def _opportunities(
    session: AsyncSession, *where: ColumnElement[bool]
) -> list[Opportunity]:
    """The eligible job runs matching `where`, in (run, attempt) order.

    Both signals come through here, so SPEC §2's eligibility table is enforced in
    one place rather than once per signal.
    """
    rows = (
        await session.execute(
            select(
                Job.id,
                Job.run_id,
                Job.run_attempt,
                Job.conclusion,
                Job.workflow_id,
                Job.head_sha,
                Job.completed_at,
                Job.step_count,
                Job.completed_step_count,
            )
            .where(*where)
            .order_by(Job.run_id, Job.run_attempt)
        )
    ).all()

    found: list[Opportunity] = []
    for job_id, run, attempt, conclusion, workflow, sha, completed_at, steps, done_steps in rows:
        outcome = job_outcome(conclusion, step_count=steps, completed_step_count=done_steps)
        if outcome is not None:
            found.append(
                Opportunity(job_id, run, attempt, outcome, workflow, sha, completed_at)
            )
    return found


async def opportunities_in_run(
    session: AsyncSession, *, repo_id: int, run_id: int, job_name: str
) -> list[Opportunity]:
    """This job name's opportunities within one run, in attempt order.

    Query: Signal A's lookup of attempts within a run — served by
    `ix_jobs_signal_a` on (repo_id, run_id, name, run_attempt).
    """
    return await _opportunities(
        session, Job.repo_id == repo_id, Job.run_id == run_id, Job.name == job_name
    )


async def opportunities_on_commit(
    session: AsyncSession, *, repo_id: int, workflow_id: int, job_name: str, head_sha: str
) -> list[Opportunity]:
    """This job name's opportunities for one workflow at one commit.

    Query: Signal B's grouping key, the hottest query in the system — served by
    `ix_jobs_signal_b` on (repo_id, workflow_id, name, head_sha), and needing no
    join because `head_sha` is denormalized onto jobs for exactly this reason.
    """
    return await _opportunities(
        session,
        Job.repo_id == repo_id,
        Job.workflow_id == workflow_id,
        Job.name == job_name,
        Job.head_sha == head_sha,
    )


async def evaluate_rerun_recovery(
    session: AsyncSession,
    *,
    repo_id: int,
    run_id: int,
    job_name: str,
    attempts: list[Opportunity] | None = None,
) -> bool:
    """Signal A: this job failed and then succeeded within the same run.

    SPEC states the rule as attempt N failing and attempt N+1 succeeding. It is
    implemented over *adjacent opportunities* rather than adjacent attempt
    numbers, which is the same thing whenever every attempt contains the job, and
    is the only reading that survives the cases the spec says to handle:
    `rerun-failed-jobs` produces an attempt containing only the jobs that failed,
    so a job can be missing from the attempt in between, and a cancelled attempt
    is not an opportunity at all. Neither should be able to hide a recovery.

    The whole group is re-derived on every call, so an attempt that arrives out of
    order still completes the picture and a replay converges on the same event.
    """
    if attempts is None:
        attempts = await opportunities_in_run(
            session, repo_id=repo_id, run_id=run_id, job_name=job_name
        )
    recoveries = [
        (failed, recovered)
        for failed, recovered in zip(attempts, attempts[1:], strict=False)
        if failed.outcome == FAILURE and recovered.outcome == SUCCESS
    ]
    if not recoveries:
        return False

    latest = recoveries[-1][1]
    evidence = {
        "head_sha": latest.head_sha,
        "attempts": [
            {"run_attempt": a.run_attempt, "job_id": a.job_id, "conclusion": a.outcome}
            for a in attempts
        ],
        "recoveries": [
            {"failed_attempt": failed.run_attempt, "recovered_attempt": recovered.run_attempt}
            for failed, recovered in recoveries
        ],
    }
    await record_flake_event(
        session,
        repo_id=repo_id,
        signal=RERUN_RECOVERY,
        job_name=job_name,
        run_id=run_id,
        evidence=evidence,
        occurred_at=latest.completed_at,
    )
    log.info(
        "detection.flake_event",
        signal=RERUN_RECOVERY,
        repo_id=repo_id,
        run_id=run_id,
        job_name=job_name,
        recoveries=len(recoveries),
    )
    return True


async def evaluate_same_commit_disagreement(
    session: AsyncSession, *, repo_id: int, workflow_id: int | None, job_name: str, head_sha: str
) -> bool:
    """Signal B: this job both passed and failed on one commit, in one workflow.

    A job whose workflow is still unknown is skipped rather than grouped, because
    SPEC §2 forbids grouping by `(job_name, head_sha)` alone — two workflows can run
    a job of the same name on the same commit and they are different jobs. Such a
    job is re-evaluated when its `workflow_run` event supplies the id (D-032).
    """
    if workflow_id is None:
        return False

    runs = await opportunities_on_commit(
        session,
        repo_id=repo_id,
        workflow_id=workflow_id,
        job_name=job_name,
        head_sha=head_sha,
    )
    outcomes = {run.outcome for run in runs}
    if not {SUCCESS, FAILURE} <= outcomes:
        return False

    evidence = {
        "job_runs": [
            {
                "run_id": run.run_id,
                "run_attempt": run.run_attempt,
                "job_id": run.job_id,
                "conclusion": run.outcome,
            }
            for run in runs
        ],
        "runs": len({run.run_id for run in runs}),
    }
    await record_flake_event(
        session,
        repo_id=repo_id,
        signal=SAME_COMMIT_DISAGREEMENT,
        job_name=job_name,
        workflow_id=workflow_id,
        head_sha=head_sha,
        evidence=evidence,
        occurred_at=max((run.completed_at for run in runs if run.completed_at), default=None),
    )
    log.info(
        "detection.flake_event",
        signal=SAME_COMMIT_DISAGREEMENT,
        repo_id=repo_id,
        workflow_id=workflow_id,
        job_name=job_name,
        head_sha=head_sha,
        job_runs=len(runs),
    )
    return True


async def record_flake_event(
    session: AsyncSession,
    *,
    repo_id: int,
    signal: str,
    job_name: str,
    evidence: dict[str, Any],
    occurred_at: datetime | None,
    workflow_id: int | None = None,
    head_sha: str | None = None,
    run_id: int | None = None,
) -> None:
    """Idempotency layer 3: unique on the grouping key plus the signal (SPEC §6).

    Signal A groups by run, so it leaves the columns Signal B groups on NULL. That
    is only a unique key because the constraint is declared NULLS NOT DISTINCT.
    Re-evaluating history therefore refreshes the evidence instead of duplicating
    the event.
    """
    stmt = insert(FlakeEvent).values(
        repo_id=repo_id,
        signal=signal,
        workflow_id=workflow_id,
        job_name=job_name,
        head_sha=head_sha,
        run_id=run_id,
        evidence=evidence,
        occurred_at=occurred_at,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_flake_events_group",
            set_={
                "evidence": stmt.excluded.evidence,
                "occurred_at": func.coalesce(stmt.excluded.occurred_at, FlakeEvent.occurred_at),
            },
        )
    )


async def evaluate_job(
    session: AsyncSession, *, repo_id: int, run_id: int, job_name: str
) -> None:
    """Re-run every signal whose answer this job's arrival could have changed.

    Called with the group rather than with the row, in the same transaction as the
    fact writes: a delivery lands its facts and their consequences together, or
    neither, and a reaped row re-derives the same result.

    Signal B's grouping key is read off the run's own job rows rather than passed in,
    so a caller only ever needs to name the job that moved.
    """
    attempts = await opportunities_in_run(
        session, repo_id=repo_id, run_id=run_id, job_name=job_name
    )
    await evaluate_rerun_recovery(
        session, repo_id=repo_id, run_id=run_id, job_name=job_name, attempts=attempts
    )
    if attempts:
        await evaluate_same_commit_disagreement(
            session,
            repo_id=repo_id,
            # Whichever attempt knows the workflow: they are attempts of one run, so
            # they share it, and a row written before the workflow was known must not
            # hide the group from the newer rows that do know it.
            workflow_id=next((a.workflow_id for a in attempts if a.workflow_id), None),
            job_name=job_name,
            head_sha=attempts[-1].head_sha,
        )
