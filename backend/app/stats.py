"""Flake rate, its confidence interval, and the leaderboard ranking.

Ranking by raw rate is the trap: one flake in two runs would
outrank fifty in a thousand. The rank key is therefore the **lower bound of the
Wilson score interval at 95% confidence**, which asks "how bad could this job be, at
worst, given how little we have seen" — so a job needs sustained evidence before it
climbs, and the interval width is itself the honest statement of what is known.

The point estimate and both bounds are stored and displayed.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import sqrt

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.detection import FAILURE, SUCCESS, opportunity_filter, outcome_expression
from app.models import FlakeEvent, Job, JobStatsDaily, Repository, Workflow

# Two-sided 95%: the z that leaves 2.5% in each tail.
Z_95 = 1.959963984540054


@dataclass(frozen=True)
class Wilson:
    """A flake rate with the interval that says how much to trust it."""

    rate: float
    lower: float
    upper: float


def wilson_interval(flakes: int, opportunities: int, z: float = Z_95) -> Wilson | None:
    """The Wilson score interval for `flakes` out of `opportunities`.

    Returns None when there are no opportunities: the interval is undefined there,
    so null is the honest answer rather than a division by zero.

    Unlike the normal approximation, this stays inside [0, 1] and stays sensible at
    p = 0 and p = 1, which is exactly where CI data lives — most jobs never flake, and
    a job seen three times and flaky three times must not be reported as certainly
    100% flaky.
    """
    if opportunities <= 0:
        return None

    n = opportunities
    p = flakes / n
    z2 = z * z
    denominator = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    margin = (z / denominator) * sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return Wilson(
        rate=p,
        lower=max(0.0, centre - margin),
        upper=min(1.0, centre + margin),
    )


@dataclass(frozen=True)
class JobFlakiness:
    """One row of the leaderboard. `interval` is None only with zero opportunities."""

    workflow_id: int | None
    job_name: str
    opportunities: int
    failures: int
    flakes: int
    last_flake_at: datetime | None
    interval: Wilson | None

    @property
    def rank_key(self) -> float:
        return self.interval.lower if self.interval else -1.0

    @property
    def identity(self) -> tuple:
        """Breaks a rank tie deterministically. Most jobs never flake, so most of a
        board is tied at a lower bound of zero and the order would otherwise be
        whatever the aggregate happened to return."""
        return (self.job_name, self.workflow_id or 0)


@dataclass(frozen=True)
class FlakeProof:
    """One failing job run a board row can be checked against on github.com.

    The public board makes a claim about a repository whose owner did not ask to be
    measured, so a row that cannot be checked in one click is not publishable. This is
    the pointer that makes it checkable: a job id and the run attempt it belongs to,
    which together address a page on github.com that shows the failure itself.
    """

    job_id: int
    run_id: int
    run_attempt: int
    head_sha: str
    conclusion: str | None
    completed_at: datetime | None


@dataclass(frozen=True)
class PublicJobFlakiness(JobFlakiness):
    """A leaderboard row from the cross-repo public board, which names its repo.

    `workflow_name` is usually null for a crawled repo and `workflow_path` is not: a
    webhook payload carries the workflow's name, while the runs listing the crawl reads
    carries only its id and file path. The board therefore has to be able to identify a
    workflow by path, because two workflows can run a job of the same name and the rows
    are otherwise indistinguishable.
    """

    repo_id: int
    repo_full_name: str
    workflow_name: str | None = None
    workflow_path: str | None = None
    proof: FlakeProof | None = None

    @property
    def identity(self) -> tuple:
        return (self.repo_full_name, *super().identity)


async def flaky_jobs(
    session: AsyncSession,
    *,
    repo_id: int,
    window_days: int = 30,
    limit: int = 50,
    now: datetime | None = None,
) -> list[JobFlakiness]:
    """The leaderboard for one repo: jobs ranked by their Wilson lower bound.

    Served from `job_stats_daily` — a window of daily rows summed, rather
    than a window of job executions scanned. The counts mean exactly what they meant
    when this read raw facts, because the rollup counts through the same
    `opportunity_filter()` and the same evidence job ids (see `app/rollup.py`); what
    changes is that the work happens once a minute instead of once a request.

    Two consequences of reading days rather than timestamps. The window is a whole
    number of UTC days, so `window_days=1` means "since the start of yesterday" rather
    than "since this time yesterday". And a job whose runs were all ineligible has a
    rollup row with zero opportunities, where the raw query simply had nothing — the
    HAVING clause is what keeps such a job off the leaderboard rather than on it with
    an undefined rate.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=window_days)).date()
    rows = (
        await session.execute(
            select(
                JobStatsDaily.workflow_id,
                JobStatsDaily.job_name,
                func.sum(JobStatsDaily.opportunities).label("opportunities"),
                func.sum(JobStatsDaily.failures).label("failures"),
                func.sum(JobStatsDaily.flakes).label("flakes"),
                func.max(JobStatsDaily.last_flake_at).label("last_flake_at"),
            )
            .where(JobStatsDaily.repo_id == repo_id, JobStatsDaily.day >= cutoff)
            .group_by(JobStatsDaily.workflow_id, JobStatsDaily.job_name)
            .having(func.sum(JobStatsDaily.opportunities) > 0)
        )
    ).all()

    return _ranked(
        [
            JobFlakiness(
                workflow_id=row.workflow_id,
                job_name=row.job_name,
                opportunities=row.opportunities,
                failures=row.failures,
                flakes=row.flakes,
                last_flake_at=row.last_flake_at,
                interval=wilson_interval(row.flakes, row.opportunities),
            )
            for row in rows
        ],
        limit,
    )


def _ranked[RowT: JobFlakiness](leaderboard: list[RowT], limit: int) -> list[RowT]:
    # Ranked by the lower bound; opportunities break a tie, because the job we have
    # watched longer is the more useful of two equally suspicious ones. Anything still
    # tied is ordered by name, so a `limit` cuts the same rows off every time — sort is
    # stable, so the first pass survives inside the second's ties.
    leaderboard.sort(key=lambda job: job.identity)
    leaderboard.sort(key=lambda job: (job.rank_key, job.opportunities), reverse=True)
    return leaderboard[:limit]


async def public_flaky_jobs(
    session: AsyncSession,
    *,
    window_days: int = 30,
    limit: int = 50,
    min_flakes: int = 0,
    now: datetime | None = None,
) -> list[PublicJobFlakiness]:
    """The cross-repo leaderboard behind `/public/flaky`: public repos only, no auth.

    **This query takes no repo id.** Which rows are visible is decided entirely by a
    join to `repositories` filtered on `private = false`, so there is no parameter a
    caller could supply to reach a private repo, and no way to forget the filter and
    still get rows back — the join is where the repo's name comes from.

    `active = false` is excluded too, and it now means **two different things** depending
    on how the repo got here — the board spans both kinds.

    For an **installed** repo it is consent: removing the App is the nearest thing to
    withdrawing it, and continuing to publish that repo's data afterwards is not
    defensible. For an **observed** repo there was never consent to withdraw, so the flag
    instead records whether the repo is still a legitimate subject — public, non-archived,
    and still there. One that went private, was archived, or was deleted is deactivated by
    the crawl and leaves the board through this same predicate.

    Both readings share the one behaviour that matters, which is why a single flag carries
    both: `active = false` means "stop publishing this", and the rows already in
    `job_stats_daily` stay rather than being deleted, because they were true when written.

    Served from the rollup like every other aggregate, so the same window rules apply
    as `flaky_jobs()` — a whole number of UTC days, current to the last sweep.

    `min_flakes` is opt-in and defaults to keeping everything, because a leaderboard
    that lists a repo's clean jobs below its flaky ones is telling the truth. It exists
    because the *page* is a different claim: a ten-row board headed "the flakiest CI"
    must not contain a job that never flaked, and the Wilson lower bound cannot be
    trusted to exclude one — `wilson_interval(0, n)` returns `3.5e-18`, not zero, so
    "the bound is positive" is not the filter it appears to be.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=window_days)).date()
    rows = (
        await session.execute(
            select(
                Repository.id.label("repo_id"),
                Repository.full_name.label("repo_full_name"),
                JobStatsDaily.workflow_id,
                Workflow.name.label("workflow_name"),
                Workflow.path.label("workflow_path"),
                JobStatsDaily.job_name,
                func.sum(JobStatsDaily.opportunities).label("opportunities"),
                func.sum(JobStatsDaily.failures).label("failures"),
                func.sum(JobStatsDaily.flakes).label("flakes"),
                func.max(JobStatsDaily.last_flake_at).label("last_flake_at"),
            )
            .join(Repository, Repository.id == JobStatsDaily.repo_id)
            # Outer: a job whose run was stubbed from a `workflow_job` payload has no
            # workflow row yet, and dropping it would silently shorten the board.
            .outerjoin(Workflow, Workflow.id == JobStatsDaily.workflow_id)
            .where(
                Repository.private.is_(False),
                Repository.active.is_(True),
                JobStatsDaily.day >= cutoff,
            )
            .group_by(
                Repository.id,
                Repository.full_name,
                JobStatsDaily.workflow_id,
                Workflow.name,
                Workflow.path,
                JobStatsDaily.job_name,
            )
            .having(
                func.sum(JobStatsDaily.opportunities) > 0,
                func.sum(JobStatsDaily.flakes) >= min_flakes,
            )
        )
    ).all()

    ranked = _ranked(
        [
            PublicJobFlakiness(
                repo_id=row.repo_id,
                repo_full_name=row.repo_full_name,
                workflow_id=row.workflow_id,
                workflow_name=row.workflow_name,
                workflow_path=row.workflow_path,
                job_name=row.job_name,
                opportunities=row.opportunities,
                failures=row.failures,
                flakes=row.flakes,
                last_flake_at=row.last_flake_at,
                interval=wilson_interval(row.flakes, row.opportunities),
            )
            for row in rows
        ],
        limit,
    )
    proofs = await _board_proofs(session, ranked)
    return [
        replace(row, proof=proofs.get((row.repo_id, row.workflow_id, row.job_name)))
        for row in ranked
    ]


async def _board_proofs(
    session: AsyncSession, rows: list[PublicJobFlakiness]
) -> dict[tuple[int, int | None, str], FlakeProof]:
    """The newest failing job run behind each ranked row, one query for the whole board.

    Recovered by expanding `flake_events.evidence.job_ids` onto `jobs` — the same
    mapping the rollup counts `flakes` through — so a proof is one of the job runs that
    put the row on the board rather than a second opinion about it. A row with no flakes
    gets no proof for exactly that reason, and needs none.

    That join, rather than the event's own columns, is also what makes a proof
    attributable to a workflow. Signal A groups by run and leaves `workflow_id` null,
    while a board row is a (workflow, job name) pair — and two workflows in one repo can
    run a job of the same name, which is not hypothetical: `ROCm/rocm-systems` has
    "Multi-Arch CI Summary" in two of them. `jobs.workflow_id` is what separates them.
    """
    if not rows:
        return {}

    implicated = (
        select(
            FlakeEvent.repo_id.label("repo_id"),
            func.jsonb_array_elements_text(FlakeEvent.evidence["job_ids"]).label("job_id"),
        )
        .where(
            FlakeEvent.repo_id.in_({row.repo_id for row in rows}),
            FlakeEvent.job_name.in_({row.job_name for row in rows}),
        )
        .subquery()
    )
    # DISTINCT ON takes the first row of each group, which the ORDER BY makes the most
    # recent failure — the one a reader checking the board would look for first.
    newest = (
        select(
            Job.repo_id,
            Job.workflow_id,
            Job.name,
            Job.id.label("job_id"),
            Job.run_id,
            Job.run_attempt,
            Job.head_sha,
            Job.conclusion,
            Job.completed_at,
        )
        .join(implicated, cast(implicated.c.job_id, BigInteger) == Job.id)
        .where(Job.conclusion.is_not(None), Job.conclusion != SUCCESS)
        .distinct(Job.repo_id, Job.workflow_id, Job.name)
        .order_by(
            Job.repo_id,
            Job.workflow_id,
            Job.name,
            Job.completed_at.desc().nullslast(),
            Job.id.desc(),
        )
    )

    return {
        (row.repo_id, row.workflow_id, row.name): FlakeProof(
            job_id=row.job_id,
            run_id=row.run_id,
            run_attempt=row.run_attempt,
            head_sha=row.head_sha,
            conclusion=row.conclusion,
            completed_at=row.completed_at,
        )
        for row in (await session.execute(newest)).all()
    }


async def flaky_jobs_from_facts(
    session: AsyncSession,
    *,
    repo_id: int,
    window_days: int = 30,
    limit: int = 50,
    now: datetime | None = None,
) -> list[JobFlakiness]:
    """The same leaderboard computed straight from the job rows — the rollup's oracle.

    Nothing in production calls this. It exists because "the rollup is correct" is
    only a claim if something independent can compute the same answer, and this is
    that something: `tests/test_rollup.py` asserts the two agree over the same window.
    Delete it only alongside that test.

    A *flaky job run* is one whose job id a signal named in its evidence. That is the
    numerator, and it counts the same thing as the denominator — a flake event is
    "an opportunity satisfying Signal A or B", so both sides are counts of job
    runs. Counting event rows instead would mix units and double-count a
    re-run recovery, which both signals legitimately report.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=window_days)

    # The job ids every signal implicated for this repo, flattened out of the evidence.
    implicated = (
        select(
            func.jsonb_array_elements_text(FlakeEvent.evidence["job_ids"]).label("job_id"),
            FlakeEvent.occurred_at.label("occurred_at"),
        )
        .where(FlakeEvent.repo_id == repo_id)
        .subquery()
    )
    flaky_runs = (
        select(
            cast(implicated.c.job_id, BigInteger).label("job_id"),
            func.max(implicated.c.occurred_at).label("occurred_at"),
        )
        .group_by(implicated.c.job_id)
        .subquery()
    )

    outcome = outcome_expression()
    rows = (
        await session.execute(
            select(
                Job.workflow_id,
                Job.name,
                func.count().label("opportunities"),
                func.count().filter(outcome == FAILURE).label("failures"),
                func.count(flaky_runs.c.job_id).label("flakes"),
                func.max(flaky_runs.c.occurred_at).label("last_flake_at"),
            )
            .outerjoin(flaky_runs, flaky_runs.c.job_id == Job.id)
            .where(
                Job.repo_id == repo_id,
                Job.completed_at >= cutoff,
                opportunity_filter(),
            )
            .group_by(Job.workflow_id, Job.name)
        )
    ).all()

    return _ranked(
        [
            JobFlakiness(
                workflow_id=row.workflow_id,
                job_name=row.name,
                opportunities=row.opportunities,
                failures=row.failures,
                flakes=row.flakes,
                last_flake_at=row.last_flake_at,
                interval=wilson_interval(row.flakes, row.opportunities),
            )
            for row in rows
        ],
        limit,
    )
