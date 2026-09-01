"""Signal A and the rows of SPEC §2's edge-case table that govern eligibility.

The first test is not a fixture invention. `build and deploy` in this project's own
repository failed on attempt 1, failed on attempt 2, and passed on attempt 3, all at
commit 995d950 — job ids, step counts, and run id below are the rows sitting in
production. **Nothing flaked.** The first two attempts failed because an IAM trust
policy was wrong and it was fixed between attempts, so an external change is what
recovered the job. Signal A cannot see that and must fire anyway (D-030): from the
Actions API alone this is indistinguishable from a flaky job, the SHA is identical
so "did the code change" does not separate them, and the spec's rule is binding.
The false positive is answered by the Wilson lower bound, which needs sustained
evidence before anything reaches the top of a leaderboard — not by a heuristic here.
"""

import zlib
from typing import Any

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import FlakeEvent, Job
from tests import payloads
from tests.helpers import deliver

RUN_ID = 33_549_797_805
JOB_NAME = "build and deploy"
SHA = "995d95020fd5070d19dd1474b66975d1b01e01b3"
ATTEMPT_JOB_IDS = (99_996_168_477, 99_997_527_370, 99_998_597_127)


def attempt(
    number: int,
    conclusion: str | None,
    *,
    job_id: int | None = None,
    name: str = JOB_NAME,
    run_id: int = RUN_ID,
    status: str = "completed",
    completed_steps: int = 14,
    total_steps: int | None = None,
) -> dict[str, Any]:
    return payloads.workflow_job(
        job_id=job_id if job_id is not None else _job_id(name, number),
        run_id=run_id,
        run_attempt=number,
        name=name,
        conclusion=conclusion,
        status=status,
        head_sha=SHA,
        completed_steps=completed_steps,
        total_steps=total_steps,
    )


def _job_id(name: str, number: int) -> int:
    """A synthetic job id, unique per (name, attempt) because it is the primary key.

    Two legs of one attempt are two separate job executions with two ids, so a
    helper that derived the id from the attempt alone would silently overwrite one
    leg with the other.
    """
    return 99_000_000_000 + number * 10_000 + zlib.crc32(name.encode()) % 10_000


async def flake_events(session) -> list[FlakeEvent]:
    return list((await session.execute(select(FlakeEvent))).scalars())


@pytest.fixture
def flag(monkeypatch):
    """Flip a config flag from SPEC §2's table and drop the settings cache."""

    def _set(name: str, value: str) -> None:
        monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Signal A
# --------------------------------------------------------------------------- #


async def test_the_production_rerun_sequence_is_a_rerun_recovery(db_session):
    """Production's own rows: failure, failure, success across three attempts."""
    await deliver(
        db_session,
        attempt(1, "failure", job_id=ATTEMPT_JOB_IDS[0]),
        attempt(2, "failure", job_id=ATTEMPT_JOB_IDS[1]),
        attempt(3, "success", job_id=ATTEMPT_JOB_IDS[2], completed_steps=18),
    )

    event = (await db_session.execute(select(FlakeEvent))).scalar_one()
    assert event.signal == "rerun_recovery"
    assert event.job_name == JOB_NAME
    assert event.run_id == RUN_ID
    # Signal A groups by run, so it leaves Signal B's grouping columns NULL.
    assert event.workflow_id is None
    assert event.head_sha is None
    assert event.occurred_at is not None

    assert event.evidence["head_sha"] == SHA
    assert [a["conclusion"] for a in event.evidence["attempts"]] == [
        "failure",
        "failure",
        "success",
    ]
    assert [a["job_id"] for a in event.evidence["attempts"]] == list(ATTEMPT_JOB_IDS)
    assert event.evidence["recoveries"] == [{"failed_attempt": 2, "recovered_attempt": 3}]


async def test_a_run_that_never_recovers_is_not_a_flake_event(db_session):
    await deliver(db_session, attempt(1, "failure"), attempt(2, "failure"))

    assert await flake_events(db_session) == []


async def test_a_job_that_passes_then_fails_is_not_a_flake_event(db_session):
    """A regression is not a flake. The direction of the transition is the signal."""
    await deliver(db_session, attempt(1, "success"), attempt(2, "failure"))

    assert await flake_events(db_session) == []


async def test_a_single_attempt_is_not_a_flake_event(db_session):
    await deliver(db_session, attempt(1, "failure"))

    assert await flake_events(db_session) == []


async def test_redelivering_the_whole_sequence_does_not_duplicate_the_event(db_session):
    """Idempotency layer 3: re-evaluating the same history converges, not duplicates."""
    sequence = (attempt(1, "failure"), attempt(2, "success"))
    await deliver(db_session, *sequence)
    first = (await db_session.execute(select(FlakeEvent))).scalar_one()

    await deliver(db_session, *sequence, *sequence)

    again = (await db_session.execute(select(FlakeEvent))).scalar_one()
    assert again.id == first.id
    assert again.created_at == first.created_at
    count = (await db_session.execute(select(func.count()).select_from(FlakeEvent))).scalar_one()
    assert count == 1


async def test_an_attempt_arriving_out_of_order_still_produces_the_event(db_session):
    """The whole group is re-derived per call, so arrival order cannot hide a recovery."""
    await deliver(db_session, attempt(2, "success"))
    assert await flake_events(db_session) == []

    await deliver(db_session, attempt(1, "failure"))

    event = (await db_session.execute(select(FlakeEvent))).scalar_one()
    assert event.evidence["recoveries"] == [{"failed_attempt": 1, "recovered_attempt": 2}]


async def test_a_rerun_of_only_the_failed_jobs_is_detected(db_session):
    """`rerun-failed-jobs` puts only the failed jobs in the new attempt.

    So the job that passed is absent from attempt 2 and must not be flagged, while
    the one that failed is adjacent to its own recovery even though a full attempt
    did not happen.
    """
    await deliver(
        db_session,
        attempt(1, "failure", name="flaky leg"),
        attempt(1, "success", name="stable leg"),
        attempt(2, "success", name="flaky leg"),
    )

    event = (await db_session.execute(select(FlakeEvent))).scalar_one()
    assert event.job_name == "flaky leg"


async def test_a_recovery_across_a_gap_in_attempts_is_detected(db_session):
    """Re-running one job by itself leaves this job absent from the attempt between.

    Adjacency is over the attempts that exist for this job, not over attempt numbers.
    """
    await deliver(db_session, attempt(1, "failure"), attempt(3, "success"))

    event = (await db_session.execute(select(FlakeEvent))).scalar_one()
    assert event.evidence["recoveries"] == [{"failed_attempt": 1, "recovered_attempt": 3}]


# --------------------------------------------------------------------------- #
# SPEC §2 edge-case table
# --------------------------------------------------------------------------- #


async def test_a_cancelled_attempt_is_not_an_opportunity(db_session):
    await deliver(db_session, attempt(1, "failure"), attempt(2, "cancelled"))
    assert await flake_events(db_session) == []

    # Excluded means invisible to the signal, so it cannot hide the recovery either.
    await deliver(db_session, attempt(3, "success"))

    event = (await db_session.execute(select(FlakeEvent))).scalar_one()
    assert event.evidence["recoveries"] == [{"failed_attempt": 1, "recovered_attempt": 3}]
    assert [a["run_attempt"] for a in event.evidence["attempts"]] == [1, 3]


async def test_a_skipped_attempt_is_not_an_opportunity(db_session):
    await deliver(db_session, attempt(1, "failure"), attempt(2, "skipped"))

    assert await flake_events(db_session) == []


async def test_an_unfinished_attempt_is_excluded_until_it_completes(db_session):
    """`conclusion: null` is not terminal — the completion event re-evaluates it."""
    await deliver(
        db_session,
        attempt(1, "failure"),
        attempt(2, None, status="in_progress", completed_steps=0, total_steps=14),
    )
    assert await flake_events(db_session) == []

    await deliver(db_session, attempt(2, "success"))

    assert len(await flake_events(db_session)) == 1


async def test_a_timed_out_attempt_counts_as_a_failure_by_default(db_session):
    await deliver(db_session, attempt(1, "timed_out"), attempt(2, "success"))

    event = (await db_session.execute(select(FlakeEvent))).scalar_one()
    assert event.evidence["attempts"][0]["conclusion"] == "failure"


async def test_a_timed_out_attempt_is_excluded_when_the_flag_says_so(db_session, flag):
    flag("TIMED_OUT_IS_FAILURE", "false")

    await deliver(db_session, attempt(1, "timed_out"), attempt(2, "success"))

    assert await flake_events(db_session) == []


async def test_a_runner_that_completed_no_steps_is_excluded_as_infrastructure(db_session):
    """Steps planned and none finished: the runner died, which is not test flakiness."""
    await deliver(
        db_session,
        attempt(1, "failure", completed_steps=0, total_steps=14),
        attempt(2, "success"),
    )

    assert await flake_events(db_session) == []
    job = (
        await db_session.execute(select(Job).where(Job.run_attempt == 1))
    ).scalar_one()
    assert (job.step_count, job.completed_step_count) == (14, 0)


async def test_an_infra_failure_is_eligible_when_the_flag_says_so(db_session, flag):
    flag("EXCLUDE_INFRA_FAILURES", "false")

    await deliver(
        db_session,
        attempt(1, "failure", completed_steps=0, total_steps=14),
        attempt(2, "success"),
    )

    assert len(await flake_events(db_session)) == 1


async def test_matrix_legs_are_different_jobs(db_session):
    """Key on the full name, matrix values included. Different legs never merge."""
    flaky = "test (ubuntu-latest, 3.11)"
    stable = "test (ubuntu-latest, 3.12)"
    await deliver(
        db_session,
        attempt(1, "failure", name=flaky),
        attempt(1, "success", name=stable),
        attempt(2, "success", name=flaky),
        attempt(2, "success", name=stable),
    )

    event = (await db_session.execute(select(FlakeEvent))).scalar_one()
    assert event.job_name == flaky


async def test_a_renamed_job_leaves_two_unrelated_histories(db_session):
    """Accept the discontinuity. No fuzzy matching between the old and new name."""
    await deliver(
        db_session,
        attempt(1, "failure", name="deploy"),
        attempt(2, "success", name="build and deploy"),
    )

    assert await flake_events(db_session) == []
