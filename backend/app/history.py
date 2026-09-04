"""One job's history, commit by commit: the timeline underneath the leaderboard.

The leaderboard says a job flakes at some rate. This says *where*: the commits it
passed on, the commits it failed on, and the commits it did both on, which is what a
flake looks like before it is averaged into a rate.

Three decisions worth stating.

**Raw job rows, not the rollup.** Every aggregate endpoint is served from
`job_stats_daily`, but that table's grain is a day and a commit is not a day, so it
could not answer this at any price. These are individual job executions, the same
exemption `/api/repos/{id}/jobs` already documents.

**A flake mark is the same job run the rate counted.** The implicated ids come from
`detection.implicated_job_ids()` through `detection.flake_filter()`: the pair
`app/rollup.py` counts its `flakes` with. So a timeline's marks sum to the
leaderboard's numerator rather than to a second opinion about it, and
`tests/test_history.py` asserts exactly that against the leaderboard's oracle.

**The commit is the group, the attempt is the row.** Signal A lives inside one
commit's attempts and Signal B lives across its runs, so a timeline that showed one
mark per commit would hide the evidence for both. Each commit therefore carries its
attempts in (run, attempt) order, and the commit's own state is derived from them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.detection import (
    FAILURE,
    SUCCESS,
    flake_filter,
    implicated_job_ids,
    opportunity_filter,
    outcome_expression,
)
from app.models import Job

# What one commit's attempts add up to. `unjudged` is not "unknown": it is a commit
# whose every attempt was cancelled, skipped, still running, or otherwise not an
# opportunity, so the job never had a chance to say anything about it.
FLAKED = "flaked"
FAILED = "failed"
PASSED = "passed"
UNJUDGED = "unjudged"


def happened_at_expression() -> ColumnElement[datetime]:
    """When a job run happened, for a query that must also see unfinished ones.

    The rollup can key on `completed_at` because a run with no completion belongs to
    no day. A timeline cannot: the newest commit on a live page is usually the one
    still running, and dropping it would make the page look stale rather than honest.
    `created_at` is the row's own insertion time and is never null, so the coalesce
    always resolves.
    """
    return func.coalesce(Job.completed_at, Job.started_at, Job.created_at)


@dataclass(frozen=True)
class Attempt:
    """One job execution. `outcome` is None when it was not an opportunity."""

    job_id: int
    run_id: int
    run_attempt: int
    workflow_id: int | None
    conclusion: str | None
    outcome: str | None
    implicated: bool
    started_at: datetime | None
    completed_at: datetime | None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()


@dataclass(frozen=True)
class CommitHistory:
    """One commit's worth of that job, newest first in the list this module returns."""

    head_sha: str
    attempts: list[Attempt]

    @property
    def state(self) -> str:
        """Flaked beats failed beats passed.

        Order matters and this is the interesting direction: a commit where the job
        failed and then passed is a flake, not a pass, and calling it either of the
        two plain states is what the whole product exists to stop.
        """
        if any(attempt.implicated for attempt in self.attempts):
            return FLAKED
        outcomes = {attempt.outcome for attempt in self.attempts}
        if FAILURE in outcomes:
            return FAILED
        if SUCCESS in outcomes:
            return PASSED
        return UNJUDGED

    @property
    def runs(self) -> int:
        """Distinct runs on this commit. More than one means Signal B's territory."""
        return len({attempt.run_id for attempt in self.attempts})

    @property
    def opportunities(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.outcome is not None)

    @property
    def failures(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.outcome == FAILURE)

    @property
    def flakes(self) -> int:
        """Implicated job *runs*, the leaderboard's unit, not incidents.

        A failure and its recovery are two implicated runs of one incident, because
        the flake rate's numerator counts runs so that it counts the same thing as
        its denominator.
        """
        return sum(1 for attempt in self.attempts if attempt.implicated)

    @property
    def first_started_at(self) -> datetime | None:
        return min((a.started_at for a in self.attempts if a.started_at), default=None)

    @property
    def last_completed_at(self) -> datetime | None:
        return max((a.completed_at for a in self.attempts if a.completed_at), default=None)


async def job_history(
    session: AsyncSession,
    *,
    repo_id: int,
    job_name: str,
    workflow_id: int | None = None,
    window_days: int = 30,
    limit: int = 30,
    now: datetime | None = None,
) -> list[CommitHistory]:
    """This job's recent commits, newest first, each with its attempts in order.

    `limit` counts **commits, not rows**, which is why the recent SHAs are chosen by
    their own aggregate before the attempts are fetched: a job re-run eleven times on
    one commit must not be able to push every other commit off a timeline of ten.

    `workflow_id` is optional and narrows rather than identifies. Two workflows can
    run a job of the same name on the same commit and they are different jobs (the
    reason Signal B groups on the workflow too), so a caller that has an id should
    pass it, and one that does not gets the union rather than an error.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=window_days)
    happened = happened_at_expression()

    scope = [Job.repo_id == repo_id, Job.name == job_name, happened >= cutoff]
    if workflow_id is not None:
        scope.append(Job.workflow_id == workflow_id)

    recent = (
        select(Job.head_sha.label("head_sha"), func.max(happened).label("last_at"))
        .where(*scope)
        .group_by(Job.head_sha)
        .order_by(func.max(happened).desc())
        .limit(limit)
        .subquery()
    )

    implicated = implicated_job_ids(repo_id)
    rows = (
        await session.execute(
            select(
                Job.head_sha,
                Job.id,
                Job.run_id,
                Job.run_attempt,
                Job.workflow_id,
                Job.conclusion,
                case((opportunity_filter(), outcome_expression())).label("outcome"),
                flake_filter(implicated).label("implicated"),
                Job.started_at,
                Job.completed_at,
                recent.c.last_at,
            )
            .join(recent, recent.c.head_sha == Job.head_sha)
            .outerjoin(implicated, implicated.c.job_id == Job.id)
            .where(*scope)
            .order_by(recent.c.last_at.desc(), Job.run_id, Job.run_attempt)
        )
    ).all()

    # The rows arrive commit-major already, so this only has to notice the boundary.
    history: list[CommitHistory] = []
    for row in rows:
        if not history or history[-1].head_sha != row.head_sha:
            history.append(CommitHistory(head_sha=row.head_sha, attempts=[]))
        history[-1].attempts.append(
            Attempt(
                job_id=row.id,
                run_id=row.run_id,
                run_attempt=row.run_attempt,
                workflow_id=row.workflow_id,
                conclusion=row.conclusion,
                outcome=row.outcome,
                implicated=row.implicated,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
        )
    return history
