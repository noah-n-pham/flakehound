"""Where the Actions minutes go, and whether jobs are getting slower.

Both reads are served from `job_stats_daily`, which is the rule for aggregates and
also the only sensible source: the rollup already computed the per-day duration
aggregates, and rescanning job rows to recompute them per request would be the same
arithmetic done worse.

Three honesty constraints shape the shapes below.

**A percentile cannot be re-aggregated.** Summing daily p50s is meaningless, and there
is no way back from a set of per-day percentiles to the window's percentile — the
underlying observations are gone. So the duration *trend* is the per-day series
verbatim, one row per day, and the minutes table reports a **mean** per run rather
than a median. A window-wide p95 would need either raw job rows or a t-digest, and
neither is worth it to avoid printing the word "mean".

**These are wall-clock seconds, not billed minutes.** GitHub bills per job, rounded up
to the minute, multiplied by a runner-type factor, and private-repo minutes come out of
an allowance that public repos do not touch. `completed_at - started_at` is what the
Actions API gives and what this reports; calling it a bill would be a lie the data
cannot support. The endpoint says `seconds` for that reason, and the page says so in
words.

**A job name is only unique inside its workflow.** Two workflows can run a job of one
name, so grouping minutes by job name alone would merge two different jobs — the same
mistake Signal B avoids by grouping on the workflow too. `group_by=job` therefore
groups on `(workflow_id, job_name)`, and the duration trend's grain is
`(workflow_id, job_name, day)`.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, null, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobStatsDaily, Workflow

GroupBy = Literal["workflow", "job"]


def _cutoff(window_days: int, now: datetime | None) -> date:
    """The window is a whole number of UTC days, because the rollup's grain is a day."""
    return ((now or datetime.now(UTC)) - timedelta(days=window_days)).date()


def _seconds(value: object) -> float:
    """`Numeric` arrives as Decimal, and a NULL sum means no finished runs, not zero."""
    return float(value) if value is not None else 0.0


@dataclass(frozen=True)
class MinutesRow:
    """One slice of the repo's Actions time over the window."""

    workflow_id: int | None
    workflow_name: str | None
    job_name: str | None
    runs: int
    seconds: float
    share: float
    mean_seconds: float | None


@dataclass(frozen=True)
class DurationPoint:
    """One day of one job's duration aggregates, exactly as the rollup stored them."""

    day: date
    workflow_id: int | None
    runs: int
    p50_seconds: float | None
    p95_seconds: float | None
    total_seconds: float


async def minutes_attribution(
    session: AsyncSession,
    *,
    repo_id: int,
    group_by: GroupBy = "workflow",
    window_days: int = 30,
    limit: int = 50,
    now: datetime | None = None,
) -> list[MinutesRow]:
    """Actions wall-clock time over the window, biggest consumer first.

    `share` is each row's fraction of the window's total, computed here rather than by
    the caller so that two pages printing "38% of your minutes" cannot disagree about
    the denominator. It is a ratio of sums, not a statistic, so there is no interval on
    it — every run in the window is counted, which is what makes it exact rather than
    estimated.

    The workflow name comes from a left join, so a job whose `workflow_run` event has
    not arrived yet still appears, attributed to a null workflow rather than dropped.
    """
    cutoff = _cutoff(window_days, now)

    # `group_by=workflow` still selects a job_name column, as NULL, so both groupings
    # return one row shape and the endpoint needs no second response model.
    grouping = [JobStatsDaily.workflow_id, Workflow.name]
    job_column = null().label("job_name")
    if group_by == "job":
        grouping.append(JobStatsDaily.job_name)
        job_column = JobStatsDaily.job_name.label("job_name")

    rows = (
        await session.execute(
            select(
                JobStatsDaily.workflow_id,
                Workflow.name.label("workflow_name"),
                job_column,
                func.sum(JobStatsDaily.runs).label("runs"),
                func.sum(JobStatsDaily.duration_total_seconds).label("seconds"),
            )
            .outerjoin(Workflow, Workflow.id == JobStatsDaily.workflow_id)
            .where(JobStatsDaily.repo_id == repo_id, JobStatsDaily.day >= cutoff)
            .group_by(*grouping)
        )
    ).all()

    total = sum(_seconds(row.seconds) for row in rows)
    attribution = [
        MinutesRow(
            workflow_id=row.workflow_id,
            workflow_name=row.workflow_name,
            job_name=row.job_name,
            runs=row.runs or 0,
            seconds=_seconds(row.seconds),
            share=(_seconds(row.seconds) / total) if total > 0 else 0.0,
            mean_seconds=(_seconds(row.seconds) / row.runs) if row.runs else None,
        )
        for row in rows
    ]
    # Biggest first, then by name so a `limit` cuts the same rows off every time — most
    # of a quiet repo's rows are tied at zero seconds.
    attribution.sort(key=lambda row: (row.workflow_name or "", row.job_name or ""))
    attribution.sort(key=lambda row: row.seconds, reverse=True)
    return attribution[:limit]


async def duration_trend(
    session: AsyncSession,
    *,
    repo_id: int,
    job_name: str,
    workflow_id: int | None = None,
    window_days: int = 30,
    now: datetime | None = None,
) -> list[DurationPoint]:
    """One job's p50 and p95 per day, oldest day first.

    The rollup's own rows, unaggregated: a percentile is not summable, so a "trend"
    is the only shape this data can honestly take.

    A day with no finished runs has no row rather than a zero — the job did not take
    zero seconds that day, it did not run. Callers draw that as a gap.

    `workflow_id` narrows to one series. Without it a job name that exists in two
    workflows returns both, ordered by workflow then day, because merging them would
    mean averaging percentiles.
    """
    cutoff = _cutoff(window_days, now)

    conditions = [
        JobStatsDaily.repo_id == repo_id,
        JobStatsDaily.job_name == job_name,
        JobStatsDaily.day >= cutoff,
    ]
    if workflow_id is not None:
        conditions.append(JobStatsDaily.workflow_id == workflow_id)

    rows = (
        await session.execute(
            select(
                JobStatsDaily.day,
                JobStatsDaily.workflow_id,
                JobStatsDaily.runs,
                JobStatsDaily.duration_p50_seconds,
                JobStatsDaily.duration_p95_seconds,
                JobStatsDaily.duration_total_seconds,
            )
            .where(*conditions)
            .order_by(JobStatsDaily.workflow_id, JobStatsDaily.day)
        )
    ).all()

    return [
        DurationPoint(
            day=row.day,
            workflow_id=row.workflow_id,
            runs=row.runs,
            p50_seconds=(
                float(row.duration_p50_seconds)
                if row.duration_p50_seconds is not None
                else None
            ),
            p95_seconds=(
                float(row.duration_p95_seconds)
                if row.duration_p95_seconds is not None
                else None
            ),
            total_seconds=_seconds(row.duration_total_seconds),
        )
        for row in rows
    ]
