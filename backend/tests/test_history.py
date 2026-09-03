"""The job history timeline: one job's commits, and what it did on each of them.

The leaderboard's claim is a rate. This module's claim is stronger and easier to
falsify: **every mark on the timeline is one of the job runs the rate counted.** The
last test in the first section asserts that against `flaky_jobs_from_facts()`, the
same independent oracle `tests/test_rollup.py` checks the rollup with, so the two
halves of the dashboard cannot tell different stories about one job.

The fixture history below is five commits of one matrix leg, one per shape the
timeline has to draw: a clean pass, a re-run recovery inside one run, a plain
failure, a commit nothing judgeable happened on, and two runs disagreeing on one
commit. The job name carries spaces, a comma and parentheses on purpose — it is a
path segment in the endpoint, and matrix names are stored whole.
"""

import zlib
from typing import Any
from urllib.parse import quote

from app.history import job_history
from app.stats import flaky_jobs_from_facts
from tests import payloads
from tests.helpers import deliver
from tests.test_api import reader

JOB_NAME = "test (ubuntu-latest, 3.11)"
WORKFLOW_ID = 73_000_003
OTHER_WORKFLOW_ID = 73_000_009


def _sha(prefix: str) -> str:
    """A 40-hex-character SHA that reads as its role in the fixture."""
    return (prefix * 40)[:40]


SHA_CLEAN = _sha("a11ce")
SHA_RERUN = _sha("b0bb1e")
SHA_FAILED = _sha("dead")
SHA_CANCELLED = _sha("cafe")
SHA_DISAGREE = _sha("fee1ed")

RUN_CLEAN = 91_000_001
RUN_RERUN = 91_000_002
RUN_FAILED = 91_000_003
RUN_CANCELLED = 91_000_004
RUN_DISAGREE_A = 91_000_005
RUN_DISAGREE_B = 91_000_006
RUN_OTHER_WORKFLOW = 91_000_007


def _job_id(run_id: int, attempt: int, name: str) -> int:
    """Unique per (run, attempt, name), because that triple is three separate rows."""
    return 96_000_000_000 + zlib.crc32(f"{run_id}:{attempt}:{name}".encode()) % 1_000_000


def run(*, sha: str, run_id: int, workflow_id: int = WORKFLOW_ID) -> dict[str, Any]:
    """The `workflow_run` delivery, which is the only event carrying the workflow id."""
    return payloads.workflow_run(run_id=run_id, workflow_id=workflow_id, head_sha=sha)


def job(
    *,
    sha: str,
    run_id: int,
    day: int,
    attempt: int = 1,
    conclusion: str | None = "success",
    name: str = JOB_NAME,
    status: str = "completed",
    completed: bool = True,
) -> dict[str, Any]:
    return payloads.workflow_job(
        job_id=_job_id(run_id, attempt, name),
        run_id=run_id,
        run_attempt=attempt,
        name=name,
        conclusion=conclusion,
        status=status,
        head_sha=sha,
        started_at=f"2026-08-{day}T14:00:00Z",
        completed_at=f"2026-08-{day}T14:05:00Z" if completed else None,
    )


async def seed_five_commits(session) -> None:
    """Five commits of one job, oldest first — one per shape the timeline draws."""
    await deliver(
        session,
        run(sha=SHA_CLEAN, run_id=RUN_CLEAN),
        job(sha=SHA_CLEAN, run_id=RUN_CLEAN, day=25),
        run(sha=SHA_RERUN, run_id=RUN_RERUN),
        job(sha=SHA_RERUN, run_id=RUN_RERUN, day=26, attempt=1, conclusion="failure"),
        job(sha=SHA_RERUN, run_id=RUN_RERUN, day=26, attempt=2, conclusion="success"),
        run(sha=SHA_FAILED, run_id=RUN_FAILED),
        job(sha=SHA_FAILED, run_id=RUN_FAILED, day=27, conclusion="failure"),
        run(sha=SHA_CANCELLED, run_id=RUN_CANCELLED),
        job(sha=SHA_CANCELLED, run_id=RUN_CANCELLED, day=28, conclusion="cancelled"),
        run(sha=SHA_DISAGREE, run_id=RUN_DISAGREE_A),
        job(sha=SHA_DISAGREE, run_id=RUN_DISAGREE_A, day=29, conclusion="failure"),
        run(sha=SHA_DISAGREE, run_id=RUN_DISAGREE_B),
        job(sha=SHA_DISAGREE, run_id=RUN_DISAGREE_B, day=29, conclusion="success"),
    )


async def history(session, **kwargs):
    return await job_history(session, repo_id=payloads.REPO_ID, job_name=JOB_NAME, **kwargs)


# --------------------------------------------------------------------------- #
# What the timeline says
# --------------------------------------------------------------------------- #


async def test_the_timeline_groups_attempts_by_commit_newest_first(db_session):
    await seed_five_commits(db_session)

    timeline = await history(db_session)

    assert [commit.head_sha for commit in timeline] == [
        SHA_DISAGREE,
        SHA_CANCELLED,
        SHA_FAILED,
        SHA_RERUN,
        SHA_CLEAN,
    ]
    assert [commit.state for commit in timeline] == [
        "flaked",
        "unjudged",
        "failed",
        "flaked",
        "passed",
    ]


async def test_a_rerun_recovery_shows_both_attempts_on_one_commit(db_session):
    """Signal A's evidence, drawn: one run, two attempts, failure then success."""
    await seed_five_commits(db_session)

    commit = next(c for c in await history(db_session) if c.head_sha == SHA_RERUN)

    assert commit.runs == 1
    assert [a.run_attempt for a in commit.attempts] == [1, 2]
    assert [a.conclusion for a in commit.attempts] == ["failure", "success"]
    assert [a.outcome for a in commit.attempts] == ["failure", "success"]
    # Both runs are implicated: the failure and the recovery are what the pair is.
    assert [a.implicated for a in commit.attempts] == [True, True]
    assert (commit.opportunities, commit.failures, commit.flakes) == (2, 1, 2)


async def test_a_disagreement_shows_two_runs_on_one_commit(db_session):
    """Signal B's evidence: two runs on one SHA, one each way, both implicated."""
    await seed_five_commits(db_session)

    commit = next(c for c in await history(db_session) if c.head_sha == SHA_DISAGREE)

    assert commit.runs == 2
    assert [a.run_id for a in commit.attempts] == [RUN_DISAGREE_A, RUN_DISAGREE_B]
    assert [a.outcome for a in commit.attempts] == ["failure", "success"]
    assert all(a.implicated for a in commit.attempts)
    assert commit.flakes == 2


async def test_a_commit_with_nothing_judgeable_is_unjudged(db_session):
    """A cancelled job says nothing about flakiness, and the timeline must not pretend.

    `unjudged` is not the same as missing: the commit is still drawn, because a gap
    in a timeline reads as data loss rather than as a cancelled run.
    """
    await seed_five_commits(db_session)

    commit = next(c for c in await history(db_session) if c.head_sha == SHA_CANCELLED)

    assert commit.state == "unjudged"
    assert commit.attempts[0].conclusion == "cancelled"
    assert commit.attempts[0].outcome is None
    assert commit.attempts[0].implicated is False
    assert (commit.opportunities, commit.failures, commit.flakes) == (0, 0, 0)


async def test_the_timeline_marks_exactly_the_job_runs_the_flake_rate_counts(db_session):
    """The claim that makes the timeline trustworthy, checked against the oracle.

    `flaky_jobs_from_facts()` computes the leaderboard independently of the rollup
    and independently of this module. If the marks summed to anything else, the page
    would be drawing a story its own ranking disagrees with.
    """
    await seed_five_commits(db_session)

    timeline = await history(db_session)
    board = await flaky_jobs_from_facts(db_session, repo_id=payloads.REPO_ID)
    ranked = next(row for row in board if row.job_name == JOB_NAME)

    assert sum(commit.flakes for commit in timeline) == ranked.flakes == 4
    assert sum(commit.opportunities for commit in timeline) == ranked.opportunities == 6
    assert sum(commit.failures for commit in timeline) == ranked.failures == 3


async def test_a_still_running_attempt_is_on_the_timeline(db_session):
    """The newest commit on a live page is usually the one still running."""
    await seed_five_commits(db_session)
    await deliver(
        db_session,
        run(sha=_sha("beef"), run_id=91_000_008),
        job(
            sha=_sha("beef"),
            run_id=91_000_008,
            day=30,
            conclusion=None,
            status="in_progress",
            completed=False,
        ),
    )

    timeline = await history(db_session)

    assert timeline[0].head_sha == _sha("beef")
    assert timeline[0].state == "unjudged"
    assert timeline[0].last_completed_at is None
    assert timeline[0].attempts[0].duration_seconds is None


async def test_the_limit_counts_commits_not_attempts(db_session):
    """A job re-run eleven times on one commit must not empty the rest of the board."""
    await seed_five_commits(db_session)

    timeline = await history(db_session, limit=2)

    assert [commit.head_sha for commit in timeline] == [SHA_DISAGREE, SHA_CANCELLED]
    # The limit cut commits off the end, not attempts off a commit.
    assert sum(len(commit.attempts) for commit in timeline) == 3


async def test_the_window_is_a_trailing_number_of_days(db_session):
    await seed_five_commits(db_session)

    assert await history(db_session, window_days=1) == []


async def test_the_same_name_in_another_workflow_is_a_different_job(db_session):
    """Two workflows can run a job of one name on one commit. They are not one job.

    Which is why the filter exists and why nothing flaked here: a failure in `ci` and
    a pass in `deploy` on the same SHA is not a disagreement, and the unfiltered
    timeline showing both attempts must still not call the commit flaked.
    """
    await seed_five_commits(db_session)
    await deliver(
        db_session,
        run(sha=SHA_CLEAN, run_id=RUN_OTHER_WORKFLOW, workflow_id=OTHER_WORKFLOW_ID),
        job(sha=SHA_CLEAN, run_id=RUN_OTHER_WORKFLOW, day=25, conclusion="failure"),
    )

    both = next(c for c in await history(db_session) if c.head_sha == SHA_CLEAN)
    assert len(both.attempts) == 2
    assert both.state == "failed"
    assert both.flakes == 0

    one = next(
        c for c in await history(db_session, workflow_id=WORKFLOW_ID) if c.head_sha == SHA_CLEAN
    )
    assert len(one.attempts) == 1
    assert one.state == "passed"


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #


def history_url(repo_id: int = payloads.REPO_ID, name: str = JOB_NAME) -> str:
    """The job name is a whole path segment, so the caller percent-encodes it."""
    return f"/api/repos/{repo_id}/jobs/{quote(name)}/history"


async def test_the_history_endpoint_returns_the_timeline(client, db_session):
    await seed_five_commits(db_session)

    response = await client.get(history_url(), headers=reader())

    assert response.status_code == 200
    body = response.json()
    assert [commit["state"] for commit in body] == [
        "flaked",
        "unjudged",
        "failed",
        "flaked",
        "passed",
    ]
    rerun = body[3]
    assert rerun["head_sha"] == SHA_RERUN
    assert rerun["runs"] == 1
    assert rerun["flakes"] == 2
    assert [a["run_attempt"] for a in rerun["attempts"]] == [1, 2]
    assert [a["implicated"] for a in rerun["attempts"]] == [True, True]
    assert rerun["attempts"][0]["duration_seconds"] == 300.0
    assert rerun["last_completed_at"] is not None


async def test_the_history_endpoint_needs_the_token(client, db_session):
    await seed_five_commits(db_session)

    response = await client.get(history_url())

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_the_history_endpoint_404s_for_a_repo_the_caller_cannot_see(client, db_session):
    await seed_five_commits(db_session)

    response = await client.get(history_url(repo_id=1_234_567), headers=reader())

    assert response.status_code == 404


async def test_a_job_name_nothing_has_ever_run_is_an_empty_timeline(client, db_session):
    """Empty rather than 404: the repo is real and the name is not a resource id."""
    await seed_five_commits(db_session)

    response = await client.get(history_url(name="no such job"), headers=reader())

    assert response.status_code == 200
    assert response.json() == []


async def test_the_endpoint_narrows_to_one_workflow_when_asked(client, db_session):
    await seed_five_commits(db_session)

    response = await client.get(
        history_url(), params={"workflow_id": OTHER_WORKFLOW_ID}, headers=reader()
    )

    assert response.status_code == 200
    assert response.json() == []


async def test_the_endpoint_rejects_a_nonsense_window(client, db_session):
    await seed_five_commits(db_session)

    response = await client.get(history_url(), params={"window_days": 0}, headers=reader())

    assert response.status_code == 422
