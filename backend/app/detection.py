"""Flake detection: the two signals that turn Actions history into evidence.

Signal A (re-run recovery) compares one job's conclusions across the attempts of a
single run. GitHub's re-run creates a new attempt under the same run id and the
commit SHA cannot change, which is what makes it the cleanest signal available:
the code provably did not change between the two conclusions.

Signal B (same-commit disagreement) compares them across every run on one commit,
which catches flakiness that shows up between separate runs (a push and a pull
request on the same SHA, or a manual re-dispatch) rather than between attempts.
A SHA is content-addressed, so a force-push cannot corrupt the grouping: identical
SHA means identical tree.

The eligibility rules from that section's edge-case table live in
`opportunity_filter`, because a signal reasons over *opportunities* rather than over
raw conclusions. A cancelled or skipped job says nothing about flakiness, and neither
does a runner that died before completing a single step. That filter is SQL rather
than Python on purpose: the leaderboard's denominator counts the same opportunities
without loading them, and two definitions of the word would eventually disagree.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Select,
    and_,
    case,
    cast,
    distinct,
    func,
    not_,
    select,
)
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
    """A terminal job run eligible for evaluation."""

    job_id: int
    run_id: int
    run_attempt: int
    outcome: str
    workflow_id: int | None
    head_sha: str
    completed_at: datetime | None


def opportunity_filter() -> ColumnElement[bool]:
    """An *opportunity*, as SQL over the jobs table.

    Every clause below is an edge case this filter has to get right:

    * `cancelled`, `skipped`, `null` (still running), and the advisory conclusions
      are simply not in the eligible set: a non-terminal job is re-evaluated when
      its completion event arrives;
    * `timed_out` is a config flag, eligible and counted as a failure by default;
    * a failure that completed none of its planned steps is a dead runner rather than
      a flaky test, and is excluded by default under its own flag. Excluding it only
      in the failure branch matters: a success is never infrastructure noise, and an
      unrecorded step count is not evidence of anything.

    This is the single definition of the word. Both signals select through it and the
    leaderboard counts through it, so the numerator and the denominator of the flake
    rate can never drift apart.
    """
    settings = get_settings()
    eligible = [SUCCESS, FAILURE]
    if settings.timed_out_is_failure:
        eligible.append("timed_out")

    filters = [Job.conclusion.in_(eligible)]
    if settings.exclude_infra_failures:
        filters.append(
            not_(
                and_(
                    Job.conclusion != SUCCESS,
                    Job.step_count.is_not(None),
                    Job.completed_step_count == 0,
                )
            )
        )
    return and_(*filters)


def outcome_expression() -> ColumnElement[str]:
    """An eligible job's outcome: `success`, or `failure` for everything else.

    Only meaningful alongside `opportunity_filter`, which is what guarantees "everything
    else" is a real failure rather than a cancellation.
    """
    return case((Job.conclusion == SUCCESS, SUCCESS), else_=FAILURE)


def implicated_job_ids(repo_id: int) -> Select:
    """The distinct job runs some signal named, flattened out of the evidence.

    One flake event stands for a whole group, so `evidence.job_ids` is the only way
    back from an event to the individual job runs it implicated. Two consumers need
    that mapping (the rollup counts its `flakes` through it and the job history marks
    its timeline with it), which is why it lives beside the signals that write it
    rather than inside either reader.

    Distinct matters: a re-run recovery records both signals, so one job id appears in
    two events, and joining the duplicates onto `jobs` would count that job run twice.
    """
    expanded = (
        select(func.jsonb_array_elements_text(FlakeEvent.evidence["job_ids"]).label("job_id"))
        .where(FlakeEvent.repo_id == repo_id)
        .subquery()
    )
    return select(distinct(cast(expanded.c.job_id, BigInteger)).label("job_id")).subquery()


def flake_filter(implicated: Select) -> ColumnElement[bool]:
    """A job row that a signal implicated *and* that was eligible to be judged.

    The eligibility half is not redundant even though every implicated run is an
    opportunity by construction: it keeps `flakes <= opportunities` a property of the
    query rather than a property of the data, so the Wilson interval can never be
    handed p > 1 by a stale event.
    """
    return and_(opportunity_filter(), implicated.c.job_id.is_not(None))


async def _opportunities(
    session: AsyncSession, *where: ColumnElement[bool]
) -> list[Opportunity]:
    """The eligible job runs matching `where`, in (run, attempt) order."""
    rows = (
        await session.execute(
            select(
                Job.id,
                Job.run_id,
                Job.run_attempt,
                outcome_expression(),
                Job.workflow_id,
                Job.head_sha,
                Job.completed_at,
            )
            .where(*where, opportunity_filter())
            .order_by(Job.run_id, Job.run_attempt)
        )
    ).all()
    return [Opportunity(*row) for row in rows]


async def opportunities_in_run(
    session: AsyncSession, *, repo_id: int, run_id: int, job_name: str
) -> list[Opportunity]:
    """This job name's opportunities within one run, in attempt order.

    Query: Signal A's lookup of attempts within a run, served by
    `ix_jobs_signal_a` on (repo_id, run_id, name, run_attempt).
    """
    return await _opportunities(
        session, Job.repo_id == repo_id, Job.run_id == run_id, Job.name == job_name
    )


async def opportunities_on_commit(
    session: AsyncSession, *, repo_id: int, workflow_id: int, job_name: str, head_sha: str
) -> list[Opportunity]:
    """This job name's opportunities for one workflow at one commit.

    Query: Signal B's grouping key, the hottest query in the system, served by
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
    is the only reading that survives every case that has to be handled:
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
        # Only the runs that form a recovery pair. A first failure that was followed by
        # another failure satisfies nothing on its own, so it is context in `attempts`
        # rather than a flake event in its own right.
        "job_ids": sorted({run.job_id for pair in recoveries for run in pair}),
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
    Grouping by `(job_name, head_sha)` alone is wrong. Two workflows can run
    a job of the same name on the same commit and they are different jobs. Such a
    job is re-evaluated when its `workflow_run` event supplies the id.
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
        "job_ids": sorted(run.job_id for run in runs),
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
    """Idempotency layer 3: unique on the grouping key plus the signal.

    Signal A groups by run, so it leaves the columns Signal B groups on NULL. That
    is only a unique key because the constraint is declared NULLS NOT DISTINCT.
    Re-evaluating history therefore refreshes the evidence instead of duplicating
    the event.

    Every caller's `evidence` carries a flat `job_ids` list beside its per-signal
    detail. One row here stands for a whole group, so that list is how the flake rate
    recovers the individual job runs the signal implicated. See `app/stats.py`.
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
