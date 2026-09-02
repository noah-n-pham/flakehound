"""Flake rate, its confidence interval, and the leaderboard ranking (SPEC §2).

Ranking by raw rate is the trap the spec calls out: one flake in two runs would
outrank fifty in a thousand. The rank key is therefore the **lower bound of the
Wilson score interval at 95% confidence**, which asks "how bad could this job be, at
worst, given how little we have seen" — so a job needs sustained evidence before it
climbs, and the interval width is itself the honest statement of what is known.

The point estimate and both bounds are stored and displayed, per SPEC §2.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import sqrt

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.detection import FAILURE, opportunity_filter, outcome_expression
from app.models import FlakeEvent, Job, JobStatsDaily

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

    Returns None when there are no opportunities: the interval is undefined there and
    SPEC §2's edge-case table says to return null rather than divide by zero.

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


async def flaky_jobs(
    session: AsyncSession,
    *,
    repo_id: int,
    window_days: int = 30,
    limit: int = 50,
    now: datetime | None = None,
) -> list[JobFlakiness]:
    """The leaderboard for one repo: jobs ranked by their Wilson lower bound.

    Served from `job_stats_daily`, per SPEC §8 — a window of daily rows summed, rather
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


def _ranked(leaderboard: list[JobFlakiness], limit: int) -> list[JobFlakiness]:
    # Ranked by the lower bound; opportunities break a tie, because the job we have
    # watched longer is the more useful of two equally suspicious ones.
    leaderboard.sort(key=lambda job: (job.rank_key, job.opportunities), reverse=True)
    return leaderboard[:limit]


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
    numerator, and it counts the same thing as the denominator — SPEC §2 defines a
    flake event as "an opportunity satisfying Signal A or B", so both sides are counts
    of job runs (D-034). Counting event rows instead would mix units and double-count a
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
