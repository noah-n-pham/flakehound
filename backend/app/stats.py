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
from app.models import FlakeEvent, Job

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

    A *flaky job run* is one whose job id a signal named in its evidence. That is the
    numerator, and it counts the same thing as the denominator — SPEC §2 defines a
    flake event as "an opportunity satisfying Signal A or B", so both sides are counts
    of job runs (D-034). Counting event rows instead would mix units and double-count a
    re-run recovery, which both signals legitimately report.

    Raw facts rather than the rollup, deliberately: `job_stats_daily` does not exist
    yet. Section E moves this query behind it, which is also what stops it from
    scanning a window of jobs on every request.
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

    leaderboard = [
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
    ]
    # Ranked by the lower bound; opportunities break a tie, because the job we have
    # watched longer is the more useful of two equally suspicious ones.
    leaderboard.sort(key=lambda job: (job.rank_key, job.opportunities), reverse=True)
    return leaderboard[:limit]
