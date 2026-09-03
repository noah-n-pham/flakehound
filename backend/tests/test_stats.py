"""The Wilson interval and the leaderboard ranked by its lower bound.

The reference values below are the published 95% Wilson intervals for those counts,
not this implementation's own output — a test that recomputed the formula would only
prove the code agrees with itself.
"""

from datetime import UTC, datetime

import pytest

from app.rollup import rollup_repository
from app.stats import flaky_jobs, wilson_interval
from tests.helpers import deliver
from tests.payloads import REPO_ID
from tests.test_detection import OTHER_RUN_ID, RUN_ID, attempt, run_event


def approx(value: float):
    return pytest.approx(value, abs=5e-5)


async def leaderboard(session, *, repo_id: int = REPO_ID, **kwargs):
    """The leaderboard as a reader sees it: rolled up first, then summed.

    The rollup is a separate pass on the worker's sweep, so a test that wants to read
    has to run it — which is the same ordering production has, one minute compressed.
    """
    await rollup_repository(session, repo_id=repo_id, now=kwargs.get("now"))
    return await flaky_jobs(session, repo_id=repo_id, **kwargs)


# --------------------------------------------------------------------------- #
# The interval
# --------------------------------------------------------------------------- #


def test_zero_opportunities_has_no_interval():
    """Undefined with no opportunities, so return null rather than divide by zero."""
    assert wilson_interval(0, 0) is None


def test_a_job_that_never_flaked_still_has_an_upper_bound():
    """Ten clean runs do not prove a rate of zero, and the interval says so."""
    interval = wilson_interval(0, 10)

    assert interval.rate == 0.0
    assert interval.lower == 0.0
    assert interval.upper == approx(0.2775)


def test_a_zero_flake_rate_does_not_reliably_give_a_zero_lower_bound():
    """Why the public board filters on `flakes` and not on the bound being positive.

    At p = 0 the centre and the margin are equal, so their difference is zero in
    arithmetic and floating-point noise here: for most denominators it cancels exactly,
    and for some — 64 opportunities, say — it leaves 3.5e-18 behind. Ranking is
    unaffected, since every real bound is many orders of magnitude larger. Filtering is
    not: "wilson_lower > 0" would put a job that never flaked on a board of flaky jobs,
    for 64 runs and not for 65, which is the kind of bug that ships.
    """
    bounds = {n: wilson_interval(0, n).lower for n in range(2, 200)}

    assert all(bound < 1e-15 for bound in bounds.values())
    assert bounds[64] > 0
    assert bounds[91] == 0.0


def test_the_published_intervals_are_reproduced():
    one_in_ten = wilson_interval(1, 10)
    assert (one_in_ten.lower, one_in_ten.upper) == (approx(0.0179), approx(0.4042))

    one_in_two = wilson_interval(1, 2)
    assert (one_in_two.lower, one_in_two.upper) == (approx(0.0945), approx(0.9055))

    fifty_in_a_thousand = wilson_interval(50, 1000)
    assert (fifty_in_a_thousand.lower, fifty_in_a_thousand.upper) == (
        approx(0.0381),
        approx(0.0653),
    )


def test_always_flaking_gives_an_upper_bound_of_exactly_one():
    """At p = 1 the centre and the margin sum to 1 exactly, so no clamping is involved."""
    interval = wilson_interval(3, 3)

    assert interval.rate == 1.0
    assert interval.upper == 1.0
    assert interval.lower == approx(0.4385)


def test_more_evidence_at_the_same_rate_raises_the_lower_bound():
    """This is the whole reason the lower bound is the rank key."""
    small = wilson_interval(5, 100)
    large = wilson_interval(50, 1000)

    assert small.rate == large.rate == 0.05
    assert small.lower < large.lower
    assert (large.upper - large.lower) < (small.upper - small.lower)


def test_the_lower_bound_dampens_the_small_sample_the_spec_warns_about():
    """SPEC's example: one flake in two runs must not simply outrank fifty in a thousand.

    Wilson compresses the gap from tenfold to roughly 2.5x rather than reversing it —
    two runs both flaking really is some evidence. What it takes to *lead* a leaderboard
    is therefore sustained evidence, but a tiny sample is not silenced, and this test
    pins the real behaviour rather than the stronger claim.
    """
    tiny = wilson_interval(1, 2)
    sustained = wilson_interval(50, 1000)

    assert tiny.rate / sustained.rate == approx(10.0)
    assert 2.0 < tiny.lower / sustained.lower < 3.0


# --------------------------------------------------------------------------- #
# The leaderboard
# --------------------------------------------------------------------------- #


async def test_the_leaderboard_ranks_a_flaky_job_above_a_clean_one(db_session):
    await deliver(
        db_session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure", name="flaky leg"),
        attempt(2, "success", name="flaky leg"),
        attempt(1, "success", name="stable leg"),
        attempt(2, "success", name="stable leg"),
    )

    board = await leaderboard(db_session)

    assert [job.job_name for job in board] == ["flaky leg", "stable leg"]

    flaky, stable = board
    assert (flaky.opportunities, flaky.failures, flaky.flakes) == (2, 1, 2)
    assert flaky.interval.rate == 1.0
    assert flaky.last_flake_at is not None

    assert (stable.opportunities, stable.failures, stable.flakes) == (2, 0, 0)
    assert stable.interval.rate == 0.0
    assert stable.interval.lower == 0.0
    assert flaky.interval.lower > stable.interval.lower


async def test_the_numerator_counts_flaky_job_runs_not_event_rows(db_session):
    """Three attempts, two of them a recovery pair, and no run event so only Signal A.

    The first failure was followed by another failure, so it satisfies nothing on its
    own and is an opportunity but not a flake.
    """
    await deliver(
        db_session,
        attempt(1, "failure"),
        attempt(2, "failure"),
        attempt(3, "success"),
    )

    job = (await leaderboard(db_session))[0]

    assert (job.opportunities, job.flakes) == (3, 2)
    assert job.interval.rate == approx(2 / 3)


async def test_both_signals_naming_one_job_run_count_it_once(db_session):
    """A re-run recovery records two events and must not be counted twice."""
    await deliver(
        db_session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure"),
        attempt(2, "success"),
    )

    job = (await leaderboard(db_session))[0]

    assert (job.opportunities, job.flakes) == (2, 2)


async def test_a_job_with_no_opportunities_is_absent_rather_than_undefined(db_session):
    """The rollup still has a row for it — two runs happened — with no opportunities."""
    await deliver(db_session, attempt(1, "cancelled"), attempt(2, "skipped"))

    assert await leaderboard(db_session) == []


async def test_the_window_excludes_older_job_runs(db_session):
    await deliver(
        db_session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure"),
        attempt(2, "success"),
    )
    assert await leaderboard(db_session, window_days=30) != []

    # The fixture payloads complete on 2026-08-31, so a one-day window in 2026-09-30
    # leaves nothing inside it.
    board = await leaderboard(
        db_session,
        window_days=1,
        now=datetime(2026, 9, 30, tzinfo=UTC),
    )
    assert board == []


async def test_another_repos_flake_events_do_not_leak_in(db_session):
    await deliver(
        db_session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure"),
        attempt(2, "success"),
    )

    assert await leaderboard(db_session, repo_id=REPO_ID + 1) == []


async def test_two_runs_disagreeing_put_every_run_in_the_group_at_risk(db_session):
    """Signal B: each run in a disagreeing group is part of a flake event."""
    await deliver(
        db_session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure", run_id=RUN_ID),
        run_event(run_id=OTHER_RUN_ID),
        attempt(1, "success", run_id=OTHER_RUN_ID),
    )

    job = (await leaderboard(db_session))[0]

    assert (job.opportunities, job.flakes) == (2, 2)
    assert job.interval.rate == 1.0
    # Two observations, so the interval stays wide: nothing is being over-claimed.
    assert job.interval.lower < 0.4
