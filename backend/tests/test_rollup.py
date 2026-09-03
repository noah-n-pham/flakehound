"""The daily rollup: what it counts, that it converges, and that it agrees with the facts.

The strongest test here is `test_the_rollup_agrees_with_the_raw_facts`, which computes
the leaderboard twice — once by summing `job_stats_daily` and once straight from the
job rows — and asserts the two are identical. Everything else in this file pins a
specific property of the recompute; that one asks whether the aggregate is the same
claim as the facts it summarises.
"""

from datetime import UTC, date, datetime

from sqlalchemy import func, select

from app.models import Job, JobStatsDaily
from app.rollup import repos_with_recent_activity, rollup_repository
from app.stats import flaky_jobs, flaky_jobs_from_facts
from app.worker import sweep
from tests.helpers import deliver, one_session_factory
from tests.payloads import REPO_ID
from tests.test_detection import OTHER_RUN_ID, RUN_ID, attempt, run_event

# Far enough after the fixture payloads (2026-08-31) that a 90-day window covers them
# and a short window does not.
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


async def rows(session) -> list[JobStatsDaily]:
    # The rollup writes through Core statements, so anything the identity map is
    # holding is a stale copy until it is expired — the same trap the queue tests hit.
    session.expire_all()
    result = await session.execute(
        select(JobStatsDaily).order_by(
            JobStatsDaily.day, JobStatsDaily.job_name, JobStatsDaily.workflow_id
        )
    )
    return list(result.scalars().all())


def snapshot(stats: list[JobStatsDaily]) -> list[tuple]:
    """Every value the rollup claims, minus the receipts."""
    return [
        (
            row.repo_id,
            row.workflow_id,
            row.job_name,
            row.day,
            row.runs,
            row.opportunities,
            row.failures,
            row.flakes,
            row.last_flake_at,
            row.duration_p50_seconds,
            row.duration_p95_seconds,
            row.duration_total_seconds,
        )
        for row in stats
    ]


async def seed_a_flaky_and_a_clean_job(session) -> None:
    await deliver(
        session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure", name="flaky leg"),
        attempt(2, "success", name="flaky leg"),
        attempt(1, "success", name="stable leg"),
        attempt(2, "success", name="stable leg"),
    )


# --------------------------------------------------------------------------- #
# What a day's row says
# --------------------------------------------------------------------------- #


async def test_a_day_counts_runs_opportunities_failures_and_flakes(db_session):
    await seed_a_flaky_and_a_clean_job(db_session)

    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    flaky, stable = sorted(await rows(db_session), key=lambda row: row.job_name)

    assert (flaky.job_name, flaky.day) == ("flaky leg", date(2026, 8, 31))
    assert (flaky.runs, flaky.opportunities, flaky.failures, flaky.flakes) == (2, 2, 1, 2)
    assert flaky.last_flake_at == datetime(2026, 8, 31, 14, 4, 12, tzinfo=UTC)

    assert (stable.runs, stable.opportunities, stable.failures, stable.flakes) == (2, 2, 0, 0)
    assert stable.last_flake_at is None


async def test_an_ineligible_run_is_counted_as_a_run_but_not_as_an_opportunity(db_session):
    """A cancelled job says nothing about flakiness, but it did execute.

    Keeping both numbers is what lets minutes attribution count every execution while
    the flake rate's denominator counts only the eligible ones.
    """
    await deliver(db_session, attempt(1, "cancelled"), attempt(2, "success"))

    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    (row,) = await rows(db_session)
    assert (row.runs, row.opportunities, row.failures, row.flakes) == (2, 1, 0, 0)


async def test_flakes_never_exceed_opportunities(db_session):
    """Both signals fire on one recovery and the same job run must count once.

    If it did not, a day could report more flakes than opportunities and the Wilson
    interval would be handed p > 1.
    """
    await seed_a_flaky_and_a_clean_job(db_session)

    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    for row in await rows(db_session):
        assert row.flakes <= row.opportunities


async def test_a_job_run_is_bucketed_by_the_utc_day_it_completed(db_session):
    await deliver(
        db_session,
        attempt(
            1,
            "failure",
            started_at="2026-08-29T23:50:00Z",
            completed_at="2026-08-29T23:59:00Z",
        ),
        attempt(
            2,
            "success",
            started_at="2026-08-30T00:01:00Z",
            completed_at="2026-08-30T00:10:00Z",
        ),
    )

    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    first, second = await rows(db_session)
    assert (first.day, first.runs, first.flakes) == (date(2026, 8, 29), 1, 1)
    assert (second.day, second.runs, second.flakes) == (date(2026, 8, 30), 1, 1)


async def test_a_flake_is_attributed_to_the_day_its_own_run_finished(db_session):
    """The recovery happened on the 30th; the attempt it redeemed ran on the 29th.

    Attributing both to the recovery's day would let a day report more flakes than it
    had opportunities. Each implicated run is counted where it actually ran, which is
    also what keeps `last_flake_at` per day meaningful.
    """
    await deliver(
        db_session,
        attempt(1, "failure", completed_at="2026-08-29T23:59:00Z"),
        attempt(2, "success", completed_at="2026-08-30T00:10:00Z"),
    )

    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    first, second = await rows(db_session)
    assert (first.flakes, first.opportunities) == (1, 1)
    assert first.last_flake_at == datetime(2026, 8, 29, 23, 59, tzinfo=UTC)
    assert (second.flakes, second.opportunities) == (1, 1)


async def test_durations_are_aggregated_over_the_days_runs(db_session):
    """Four runs of 1, 2, 3 and 4 minutes: p50 is the midpoint, p95 interpolates."""
    for index, minutes in enumerate((1, 2, 3, 4), start=1):
        await deliver(
            db_session,
            attempt(
                index,
                "success",
                started_at="2026-08-31T14:00:00Z",
                completed_at=f"2026-08-31T14:0{minutes}:00Z",
            ),
        )

    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    (row,) = await rows(db_session)
    assert float(row.duration_total_seconds) == 600.0
    # percentile_cont interpolates: p50 sits between 120 and 180, p95 between 180 and 240.
    assert float(row.duration_p50_seconds) == 150.0
    assert float(row.duration_p95_seconds) == 231.0


async def test_a_job_that_has_not_finished_belongs_to_no_day(db_session):
    """It is re-rolled when its completion event lands, so it is absent rather than zero."""
    await deliver(
        db_session,
        attempt(1, "success"),
        attempt(2, None, status="in_progress", completed_at=None),
    )

    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    (row,) = await rows(db_session)
    assert row.runs == 1


async def test_another_repos_jobs_do_not_leak_in(db_session):
    await seed_a_flaky_and_a_clean_job(db_session)

    await rollup_repository(db_session, repo_id=REPO_ID + 1, now=NOW)

    assert await rows(db_session) == []


# --------------------------------------------------------------------------- #
# Convergence
# --------------------------------------------------------------------------- #


async def test_rolling_up_twice_changes_nothing(db_session):
    """Idempotency: a day is recomputed from the facts, never incremented."""
    await seed_a_flaky_and_a_clean_job(db_session)

    first = await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)
    before = await rows(db_session)
    before_snapshot, before_ids = snapshot(before), [row.id for row in before]

    second = await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)
    after = await rows(db_session)

    assert (first.written, first.removed) == (2, 0)
    assert (second.written, second.removed) == (2, 0)
    assert snapshot(after) == before_snapshot
    # Updated in place rather than deleted and re-inserted, so a row keeps its identity
    # and `created_at` still says when this day was first summarised.
    assert [row.id for row in after] == before_ids


async def test_late_history_is_picked_up_by_the_next_recompute(db_session):
    """What a backfill does: facts appear for a day that was already rolled up."""
    await deliver(db_session, attempt(1, "success"))
    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)
    assert (await rows(db_session))[0].runs == 1

    await deliver(db_session, attempt(2, "failure"), attempt(3, "success"))
    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    (row,) = await rows(db_session)
    assert (row.runs, row.opportunities, row.failures, row.flakes) == (3, 3, 1, 2)


async def test_a_regrouped_day_leaves_no_stale_row_behind(db_session):
    """A job's workflow arrives late, so its day regroups under a new key.

    The old NULL-workflow row has to go, or the leaderboard sums the day twice.
    """
    await deliver(db_session, attempt(1, "failure"), attempt(2, "success"))
    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    (row,) = await rows(db_session)
    assert row.workflow_id is None
    before = (row.runs, row.opportunities, row.flakes)

    await deliver(db_session, run_event(run_id=RUN_ID))
    result = await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    (after,) = await rows(db_session)
    assert after.workflow_id is not None
    assert (after.runs, after.opportunities, after.flakes) == before
    assert (result.written, result.removed) == (1, 1)


async def test_a_day_outside_the_window_is_left_alone(db_session):
    """The recompute owns its window and nothing older, so old history is not deleted."""
    await deliver(db_session, attempt(1, "success"))
    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    # A window starting after the fixture day: nothing to recompute, nothing to remove.
    result = await rollup_repository(db_session, repo_id=REPO_ID, days=1, now=NOW)

    assert (result.written, result.removed) == (0, 0)
    assert len(await rows(db_session)) == 1


# --------------------------------------------------------------------------- #
# The rollup against the facts it summarises
# --------------------------------------------------------------------------- #


async def test_the_rollup_agrees_with_the_raw_facts(db_session):
    """The leaderboard summed from days equals the leaderboard scanned from jobs.

    Two signals, two workflows, two runs on one commit, a cancelled run and an
    unfinished one — the awkward cases in one dataset, so the agreement is not a
    coincidence of a trivial fixture.
    """
    await deliver(
        db_session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure", name="flaky leg"),
        attempt(2, "success", name="flaky leg"),
        attempt(1, "success", name="stable leg"),
        attempt(1, "cancelled", name="cancelled leg"),
        attempt(2, None, name="running leg", status="in_progress", completed_at=None),
        run_event(run_id=OTHER_RUN_ID),
        attempt(1, "failure", name="stable leg", run_id=OTHER_RUN_ID),
    )

    await rollup_repository(db_session, repo_id=REPO_ID, now=NOW)

    from_rollup = await flaky_jobs(db_session, repo_id=REPO_ID, window_days=90, now=NOW)
    from_facts = await flaky_jobs_from_facts(db_session, repo_id=REPO_ID, window_days=90, now=NOW)

    def comparable(board):
        return [
            (job.workflow_id, job.job_name, job.opportunities, job.failures, job.flakes)
            for job in board
        ]

    assert comparable(from_rollup) == comparable(from_facts)
    assert [job.interval for job in from_rollup] == [job.interval for job in from_facts]
    # `last_flake_at` is the one value the two derive differently — the rollup takes the
    # implicated run's own completion, the oracle the event's occurrence — so the claim
    # is that they agree about *whether* a job has ever flaked.
    assert [job.last_flake_at is None for job in from_rollup] == [
        job.last_flake_at is None for job in from_facts
    ]


async def test_the_leaderboard_is_empty_until_the_rollup_has_run(db_session):
    """Reads come from the rollup, so this is the honest failure mode: stale, not wrong."""
    await seed_a_flaky_and_a_clean_job(db_session)

    assert await flaky_jobs(db_session, repo_id=REPO_ID, now=NOW) == []
    assert await flaky_jobs_from_facts(db_session, repo_id=REPO_ID, now=NOW) != []


# --------------------------------------------------------------------------- #
# Which repos the sweep recomputes
# --------------------------------------------------------------------------- #


async def test_only_repos_whose_jobs_moved_are_recomputed(db_session):
    await seed_a_flaky_and_a_clean_job(db_session)

    touched = await db_session.scalar(select(func.max(Job.updated_at)))

    assert await repos_with_recent_activity(db_session, touched) == [REPO_ID]
    assert await repos_with_recent_activity(db_session, datetime.now(UTC)) == []


async def test_the_workers_sweep_is_what_keeps_the_leaderboard_current(db_session):
    """End to end: deliveries land, the sweep runs, and the read endpoint has data."""
    await seed_a_flaky_and_a_clean_job(db_session)
    assert await flaky_jobs(db_session, repo_id=REPO_ID) == []

    result = await sweep(one_session_factory(db_session))

    assert result.rolled_up == [REPO_ID]
    assert [job.job_name for job in await flaky_jobs(db_session, repo_id=REPO_ID)] == [
        "flaky leg",
        "stable leg",
    ]
