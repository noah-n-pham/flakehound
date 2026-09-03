"""Minutes attribution and the duration trend, both served from the rollup.

The fixture is two workflows over three days with durations chosen so every number
below is checkable by hand: `ci` burns 900 seconds, `deploy` burns 300, so the shares
are exactly 75% and 25% and nothing here depends on a coincidence.

What these tests are really guarding is the pair of things the shapes exist to prevent:
a job name merged across two workflows, and a percentile treated as if it summed.
"""

from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from app.rollup import rollup_repository
from app.usage import duration_trend, minutes_attribution
from tests import payloads
from tests.helpers import deliver
from tests.test_api import reader

# Fixed so the window arithmetic is not "whatever today is".
NOW = datetime.fromisoformat("2026-08-31T12:00:00+00:00")

CI_WORKFLOW = 73_000_101
DEPLOY_WORKFLOW = 73_000_202
JOB_NAME = "test (ubuntu-latest, 3.11)"
SHARED_NAME = "build"


def run(*, run_id: int, workflow_id: int, workflow_name: str, sha: str) -> dict[str, Any]:
    return payloads.workflow_run(
        run_id=run_id, workflow_id=workflow_id, workflow_name=workflow_name, head_sha=sha
    )


def job(
    *,
    run_id: int,
    day: int,
    seconds: int,
    name: str = JOB_NAME,
    job_id: int | None = None,
    sha: str = payloads.SHA,
) -> dict[str, Any]:
    """One finished job run of a known length, on a known UTC day."""
    minutes, remainder = divmod(seconds, 60)
    return payloads.workflow_job(
        job_id=job_id if job_id is not None else 97_000_000_000 + run_id * 10 + day,
        run_id=run_id,
        name=name,
        head_sha=sha,
        started_at=f"2026-08-{day}T10:00:00Z",
        completed_at=f"2026-08-{day}T10:{minutes:02d}:{remainder:02d}Z",
    )


async def seed_two_workflows(session) -> None:
    """`ci` takes 900 seconds across three days; `deploy` takes 300 on one."""
    await deliver(
        session,
        run(run_id=95_001, workflow_id=CI_WORKFLOW, workflow_name="ci", sha=payloads.SHA),
        job(run_id=95_001, day=28, seconds=120),
        job(run_id=95_001, day=29, seconds=180, job_id=97_100_000_001),
        job(run_id=95_001, day=30, seconds=600, job_id=97_100_000_002),
        run(
            run_id=95_002,
            workflow_id=DEPLOY_WORKFLOW,
            workflow_name="deploy",
            sha=payloads.SHA,
        ),
        job(run_id=95_002, day=30, seconds=300, name=SHARED_NAME, job_id=97_200_000_001),
    )
    await rollup_repository(session, repo_id=payloads.REPO_ID, now=NOW)


# --------------------------------------------------------------------------- #
# Minutes attribution
# --------------------------------------------------------------------------- #


async def test_minutes_are_attributed_to_workflows_biggest_first(db_session):
    await seed_two_workflows(db_session)

    rows = await minutes_attribution(db_session, repo_id=payloads.REPO_ID, now=NOW)

    assert [row.workflow_name for row in rows] == ["ci", "deploy"]
    assert [row.seconds for row in rows] == [900.0, 300.0]
    assert [row.share for row in rows] == [0.75, 0.25]
    assert [row.runs for row in rows] == [3, 1]
    # Grouping by workflow says nothing about which job, and says so with a null.
    assert [row.job_name for row in rows] == [None, None]
    assert rows[0].mean_seconds == 300.0


async def test_grouping_by_job_keeps_the_workflow_in_the_key(db_session):
    """A job name is only unique inside its workflow, so the group has to carry both."""
    await seed_two_workflows(db_session)
    await deliver(
        db_session,
        # The same job name as `deploy`'s, but in `ci`. Two jobs, not one.
        job(run_id=95_001, day=30, seconds=60, name=SHARED_NAME, job_id=97_100_000_003),
    )
    await rollup_repository(db_session, repo_id=payloads.REPO_ID, now=NOW)

    rows = await minutes_attribution(
        db_session, repo_id=payloads.REPO_ID, group_by="job", now=NOW
    )

    shared = [row for row in rows if row.job_name == SHARED_NAME]
    assert len(shared) == 2
    assert {(row.workflow_name, row.seconds) for row in shared} == {
        ("ci", 60.0),
        ("deploy", 300.0),
    }


async def test_shares_are_a_fraction_of_the_window_not_of_all_time(db_session):
    """A window that holds only the last day holds only that day's seconds."""
    await seed_two_workflows(db_session)

    rows = await minutes_attribution(
        db_session, repo_id=payloads.REPO_ID, window_days=1, now=NOW
    )

    # NOW is 2026-08-31, so a one-day window starts at 2026-08-30: 600 + 300 seconds.
    assert [(row.workflow_name, row.seconds, row.share) for row in rows] == [
        ("ci", 600.0, 600 / 900),
        ("deploy", 300.0, 300 / 900),
    ]


async def test_a_repo_with_no_finished_runs_has_no_shares_to_divide(db_session):
    """Zero total must not be a division by zero, and must not be 100% of nothing."""
    rows = await minutes_attribution(db_session, repo_id=payloads.REPO_ID, now=NOW)

    assert rows == []


# --------------------------------------------------------------------------- #
# Duration trend
# --------------------------------------------------------------------------- #


async def test_the_trend_is_one_row_per_day_oldest_first(db_session):
    await seed_two_workflows(db_session)

    trend = await duration_trend(
        db_session, repo_id=payloads.REPO_ID, job_name=JOB_NAME, now=NOW
    )

    assert [point.day for point in trend] == [
        date(2026, 8, 28),
        date(2026, 8, 29),
        date(2026, 8, 30),
    ]
    # One run a day, so each day's p50 and p95 are that run's own duration.
    assert [point.p50_seconds for point in trend] == [120.0, 180.0, 600.0]
    assert [point.p95_seconds for point in trend] == [120.0, 180.0, 600.0]
    assert [point.runs for point in trend] == [1, 1, 1]
    assert all(point.workflow_id == CI_WORKFLOW for point in trend)


async def test_a_day_the_job_did_not_run_is_absent_rather_than_zero(db_session):
    """A gap is not a fast day. The series omits it and the caller draws the gap."""
    await seed_two_workflows(db_session)

    trend = await duration_trend(
        db_session, repo_id=payloads.REPO_ID, job_name=SHARED_NAME, now=NOW
    )

    assert [point.day for point in trend] == [date(2026, 8, 30)]
    assert trend[0].total_seconds == 300.0


async def test_the_trend_narrows_to_one_workflow_when_asked(db_session):
    """Two workflows running one job name are two series, never one averaged series."""
    await seed_two_workflows(db_session)
    await deliver(
        db_session,
        job(run_id=95_001, day=29, seconds=60, name=SHARED_NAME, job_id=97_100_000_004),
    )
    await rollup_repository(db_session, repo_id=payloads.REPO_ID, now=NOW)

    both = await duration_trend(
        db_session, repo_id=payloads.REPO_ID, job_name=SHARED_NAME, now=NOW
    )
    assert {point.workflow_id for point in both} == {CI_WORKFLOW, DEPLOY_WORKFLOW}

    one = await duration_trend(
        db_session,
        repo_id=payloads.REPO_ID,
        job_name=SHARED_NAME,
        workflow_id=DEPLOY_WORKFLOW,
        now=NOW,
    )
    assert [(point.day, point.p50_seconds) for point in one] == [(date(2026, 8, 30), 300.0)]


async def test_the_trend_and_the_attribution_agree_on_total_seconds(db_session):
    """Two reads of one rollup, so a mismatch would mean one of them is filtering wrong."""
    await seed_two_workflows(db_session)

    trend = await duration_trend(
        db_session, repo_id=payloads.REPO_ID, job_name=JOB_NAME, now=NOW
    )
    rows = await minutes_attribution(
        db_session, repo_id=payloads.REPO_ID, group_by="job", now=NOW
    )
    attributed = next(row for row in rows if row.job_name == JOB_NAME)

    assert sum(point.total_seconds for point in trend) == attributed.seconds == 900.0


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #


async def test_the_minutes_endpoint_returns_attribution(client, db_session):
    await seed_two_workflows(db_session)

    response = await client.get(f"/api/repos/{payloads.REPO_ID}/minutes", headers=reader())

    assert response.status_code == 200
    body = response.json()
    assert [(row["workflow_name"], row["seconds"], row["share"]) for row in body] == [
        ("ci", 900.0, 0.75),
        ("deploy", 300.0, 0.25),
    ]


async def test_the_minutes_endpoint_rejects_an_unknown_grouping(client, db_session):
    await seed_two_workflows(db_session)

    response = await client.get(
        f"/api/repos/{payloads.REPO_ID}/minutes",
        params={"group_by": "runner"},
        headers=reader(),
    )

    assert response.status_code == 422


async def test_the_minutes_endpoint_needs_the_token(client, db_session):
    await seed_two_workflows(db_session)

    response = await client.get(f"/api/repos/{payloads.REPO_ID}/minutes")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_the_minutes_endpoint_404s_for_a_repo_the_caller_cannot_see(client, db_session):
    await seed_two_workflows(db_session)

    response = await client.get("/api/repos/1234567/minutes", headers=reader())

    assert response.status_code == 404


async def test_the_duration_endpoint_returns_the_series(client, db_session):
    await seed_two_workflows(db_session)

    response = await client.get(
        f"/api/repos/{payloads.REPO_ID}/jobs/{quote(JOB_NAME)}/duration",
        params={"workflow_id": CI_WORKFLOW},
        headers=reader(),
    )

    assert response.status_code == 200
    body = response.json()
    assert [point["day"] for point in body] == ["2026-08-28", "2026-08-29", "2026-08-30"]
    assert [point["p95_seconds"] for point in body] == [120.0, 180.0, 600.0]


async def test_the_duration_endpoint_needs_the_token(client, db_session):
    await seed_two_workflows(db_session)

    response = await client.get(
        f"/api/repos/{payloads.REPO_ID}/jobs/{quote(JOB_NAME)}/duration"
    )

    assert response.status_code == 401


async def test_the_duration_and_history_paths_do_not_shadow_each_other(client, db_session):
    """Both take the job name as a `:path` segment, so the suffix is what distinguishes them."""
    await seed_two_workflows(db_session)

    duration = await client.get(
        f"/api/repos/{payloads.REPO_ID}/jobs/{quote(JOB_NAME)}/duration", headers=reader()
    )
    history = await client.get(
        f"/api/repos/{payloads.REPO_ID}/jobs/{quote(JOB_NAME)}/history", headers=reader()
    )

    assert duration.status_code == history.status_code == 200
    assert "day" in duration.json()[0]
    assert "head_sha" in history.json()[0]
