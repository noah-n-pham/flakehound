"""The daily rollup — the aggregate every read endpoint is served from.

One row per (repo, workflow, job name, UTC day), holding runs, failures,
opportunities, flakes, and duration aggregates including p50
and p95. The leaderboard then sums a window of these rows instead of scanning a
window of job executions.

**A day is recomputed from the facts, never incremented.** That is what makes it
idempotent in the only sense that matters here: running it twice, or ten times, or
after a backfill has filled in three-month-old history, converges on the same
numbers. An incremental counter would need every write path to remember to move it
and would drift the first time a delivery was replayed — which the reaper guarantees
will happen.

**And the whole trailing window is recomputed, not just today.** Flake attribution
reaches backwards in time. A run can be re-run 30 days later, and the attempt that
failed back then only becomes a flake when today's re-run
succeeds. Tracking which old days a new event dirtied would mean modelling that
reach; recomputing the window is one aggregate query over one repo and cannot get it
wrong. Revisit if a repo's window ever costs enough to measure.

Each count is deliberate, because the leaderboard's numerator and denominator have
to keep counting the same unit:

* `runs` — every job execution that finished that day, whatever its conclusion.
  This is the unit minutes attribution and the duration trend are about.
* `opportunities` — those runs that pass `opportunity_filter()`, the eligible set.
  The denominator of the flake rate.
* `failures` — opportunities whose outcome is a failure.
* `flakes` — opportunities a signal named in its evidence, via
  `detection.implicated_job_ids()`. One flake event stands for a whole group, so the
  job ids in `evidence.job_ids` are what recovers the individual job runs, and a run
  named by both signals is counted once. Every implicated run is an opportunity by
  construction, so `flakes <= opportunities` holds for every row and the Wilson
  interval can never see p > 1.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import (
    Date,
    Numeric,
    Select,
    and_,
    cast,
    delete,
    extract,
    func,
    literal,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.detection import (
    FAILURE,
    flake_filter,
    implicated_job_ids,
    opportunity_filter,
    outcome_expression,
)
from app.logging import configure_logging, get_logger
from app.models import Job, JobStatsDaily, Repository

log = get_logger(__name__)

# The columns `_aggregate` produces, in order, matching the INSERT below.
_COLUMNS = (
    "repo_id",
    "workflow_id",
    "job_name",
    "day",
    "runs",
    "opportunities",
    "failures",
    "flakes",
    "last_flake_at",
    "duration_p50_seconds",
    "duration_p95_seconds",
    "duration_total_seconds",
)

# Everything a recompute overwrites. `created_at` is deliberately absent: the row's
# first appearance is a fact about the rollup's own history, not about the day.
_KEY = ("repo_id", "workflow_id", "job_name", "day")
_REFRESHED = tuple(name for name in _COLUMNS if name not in _KEY)


@dataclass(frozen=True)
class RollupResult:
    repo_id: int
    since: date
    written: int
    removed: int


def day_expression():
    """The UTC calendar day a job run belongs to.

    UTC rather than a repo-local timezone because GitHub's timestamps are UTC and a
    day boundary that moved with the reader would make two summaries of one window
    disagree.
    """
    return cast(func.timezone("UTC", Job.completed_at), Date)


def duration_expression():
    """Wall-clock seconds a job execution took, or NULL if it never started."""
    return extract("epoch", Job.completed_at - Job.started_at)


def _aggregate(repo_id: int, since: date) -> Select:
    """One row per (workflow, job name, day) for this repo, computed from raw facts."""
    implicated = implicated_job_ids(repo_id)
    day = day_expression()
    duration = duration_expression()

    opportunity = opportunity_filter()
    failed = and_(opportunity, outcome_expression() == FAILURE)
    flaky = flake_filter(implicated)

    return (
        select(
            literal(repo_id),
            Job.workflow_id,
            Job.name,
            day,
            func.count(),
            func.count().filter(opportunity),
            func.count().filter(failed),
            func.count().filter(flaky),
            func.max(Job.completed_at).filter(flaky),
            cast(func.percentile_cont(0.5).within_group(duration), Numeric(12, 3)),
            cast(func.percentile_cont(0.95).within_group(duration), Numeric(12, 3)),
            cast(func.sum(duration), Numeric(14, 3)),
        )
        .select_from(Job)
        .outerjoin(implicated, implicated.c.job_id == Job.id)
        .where(
            Job.repo_id == repo_id,
            # A job with no completion belongs to no day. It is still running, or it
            # is a queued row waiting for its completion event, and either way it is
            # re-rolled when that event lands.
            Job.completed_at.is_not(None),
            day >= since,
        )
        .group_by(Job.workflow_id, Job.name, day)
    )


async def rollup_repository(
    session: AsyncSession,
    *,
    repo_id: int,
    days: int | None = None,
    now: datetime | None = None,
) -> RollupResult:
    """Recompute one repo's trailing window of daily stats. Caller commits.

    Stale rows in the window are deleted rather than left behind, because a row's
    grouping key can move: a job whose `workflow_id` was still unknown is rolled up
    under NULL, and when the `workflow_run` event supplies the id the same
    job runs regroup under it. Without the delete, both rows would be summed and the
    leaderboard would show the day twice.
    """
    settings = get_settings()
    since = ((now or datetime.now(UTC)) - timedelta(days=days or settings.rollup_days)).date()

    stmt = insert(JobStatsDaily).from_select(list(_COLUMNS), _aggregate(repo_id, since))
    written = (
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_job_stats_daily_key",
                set_={name: getattr(stmt.excluded, name) for name in _REFRESHED}
                | {"updated_at": func.now()},
            ).returning(JobStatsDaily.id)
        )
    ).scalars().all()

    removed = (
        await session.execute(
            delete(JobStatsDaily).where(
                JobStatsDaily.repo_id == repo_id,
                JobStatsDaily.day >= since,
                JobStatsDaily.id.notin_(written),
            )
        )
    ).rowcount

    log.info(
        "rollup.repository",
        repo_id=repo_id,
        since=since.isoformat(),
        written=len(written),
        removed=removed,
    )
    return RollupResult(repo_id=repo_id, since=since, written=len(written), removed=removed)


async def repos_with_recent_activity(session: AsyncSession, since: datetime) -> list[int]:
    """Repos whose job rows moved since `since` — the ones whose rollup is stale.

    Query: served by `ix_jobs_recent_activity` on jobs(updated_at).
    """
    rows = await session.execute(
        select(Job.repo_id).where(Job.updated_at >= since).group_by(Job.repo_id)
    )
    return list(rows.scalars().all())


async def all_repo_ids(session: AsyncSession) -> list[int]:
    rows = await session.execute(select(Repository.id).order_by(Repository.id))
    return list(rows.scalars().all())


async def rollup_repos(
    sessionmaker: async_sessionmaker, repo_ids: list[int]
) -> list[RollupResult]:
    """Roll each repo up in its own transaction, so one failure cannot lose the rest."""
    results = []
    for repo_id in repo_ids:
        async with sessionmaker() as session:
            results.append(await rollup_repository(session, repo_id=repo_id))
            await session.commit()
    return results


async def rollup_recent(sessionmaker: async_sessionmaker, *, since: datetime) -> list[RollupResult]:
    """The worker's pass: recompute every repo that has seen a job write since `since`."""
    async with sessionmaker() as session:
        repo_ids = await repos_with_recent_activity(session, since)
    return await rollup_repos(sessionmaker, repo_ids)


async def _run(repo_id: int | None, days: int | None) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        repo_ids = [repo_id] if repo_id is not None else await all_repo_ids(session)
    for target in repo_ids:
        async with sessionmaker() as session:
            await rollup_repository(session, repo_id=target, days=days)
            await session.commit()
    await dispose_engine()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Recompute daily job stats from raw facts.")
    parser.add_argument("--repo-id", type=int, default=None, help="one repo; default is all")
    parser.add_argument("--days", type=int, default=None, help="trailing window, default 90")
    args = parser.parse_args()
    asyncio.run(_run(args.repo_id, args.days))


if __name__ == "__main__":
    main()
