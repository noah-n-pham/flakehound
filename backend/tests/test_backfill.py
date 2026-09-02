"""The date-windowed backfill and its resumable cursor (SPEC §7).

GitHub is respx throughout. The database is the real one, because the cursor is
a database row and the whole point of the design is that it survives.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select, update

from app.backfill import (
    JOBS_JOB_TYPE,
    RUNS_JOB_TYPE,
    Window,
    first_window,
    handle_backfill_jobs,
    handle_backfill_runs,
    next_window,
    start_backfill,
)
from app.config import get_settings
from app.github import reset_api_state, reset_token_cache
from app.models import EventQueue, FlakeEvent, Job, Repository, WorkflowRun

INSTALLATION_ID = 158_221_992
REPO_ID = 1_352_471_967
FULL_NAME = "noah-n-pham/flakehound"
WORKFLOW_ID = 347_813_653
TOKEN_URL = f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
RUNS_URL = f"https://api.github.com/repos/{FULL_NAME}/actions/runs"


# --------------------------------------------------------------------------- #
# The window walk — pure, so it is tested without a database or a network
# --------------------------------------------------------------------------- #


def test_the_first_window_is_the_newest_one():
    window = first_window(today=date(2026, 9, 2), days=90, window_days=7)

    assert window == Window(start=date(2026, 8, 27), end=date(2026, 9, 2))
    assert window.as_filter() == "2026-08-27..2026-09-02"


def test_a_short_history_is_one_window_that_stops_at_the_floor():
    window = first_window(today=date(2026, 9, 2), days=3, window_days=7)

    assert window == Window(start=date(2026, 8, 31), end=date(2026, 9, 2))


def test_windows_tile_the_history_with_no_gap_and_no_overlap():
    """Every day between the floor and today is covered exactly once.

    Counting days rather than eyeballing boundaries: an off-by-one at a window
    edge is the failure this design is most exposed to, and it would lose a day
    of history silently.
    """
    today = date(2026, 9, 2)
    floor = today - timedelta(days=89)
    window = first_window(today=today, days=90, window_days=7)

    covered: list[date] = []
    # Bounded, because a walk that fails to terminate is a real defect and a test
    # that hangs reports nothing. A window that overlaps its predecessor by one
    # day never reaches the floor, which is exactly how this loop found out.
    for _ in range(200):
        if window is None:
            break
        day = window.start
        while day <= window.end:
            covered.append(day)
            day += timedelta(days=1)
        window = next_window(window, floor=floor, window_days=7)
    else:
        pytest.fail("the window walk did not reach the floor")

    assert len(covered) == 90
    assert len(set(covered)) == 90
    assert min(covered) == floor
    assert max(covered) == today


def test_halving_keeps_the_newest_half_and_leaves_no_gap_behind_it():
    """A narrowed window must hand the days it gave up to the next window.

    Narrowing moves `start` forward and never touches `end`, and the next
    window's `end` is derived from this one's `start` minus a day — so the two
    still meet whatever the halving did.
    """
    window = Window(start=date(2026, 8, 27), end=date(2026, 9, 2))

    narrowed = window.halved()

    assert narrowed == Window(start=date(2026, 8, 30), end=date(2026, 9, 2))
    following = next_window(narrowed, floor=date(2026, 6, 5), window_days=7)
    assert following.end == date(2026, 8, 29)
    assert following.end + timedelta(days=1) == narrowed.start


def test_halving_bottoms_out_at_a_single_day():
    window = Window(start=date(2026, 9, 2), end=date(2026, 9, 2))

    assert window.span_days == 0
    assert window.halved() == window


def test_the_walk_ends_at_the_floor():
    floor = date(2026, 6, 5)
    assert next_window(Window(start=floor, end=floor), floor=floor, window_days=7) is None


# --------------------------------------------------------------------------- #
# Fixtures for the handlers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def keypair() -> str:
    """A throwaway key. The real one at `~/flakehound-app.pem` is never read here."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )


@pytest.fixture
def github(monkeypatch, keypair):
    """Credentials plus a mocked token exchange, and small backfill numbers.

    A 7-day history in 3-day windows with 2 results a page exercises every
    branch — more than one page, more than one window, and the floor — in a
    handful of requests.
    """
    monkeypatch.setenv("GITHUB_APP_ID", "4792446")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", keypair)
    monkeypatch.setenv("BACKFILL_DAYS", "7")
    monkeypatch.setenv("BACKFILL_WINDOW_DAYS", "3")
    monkeypatch.setenv("BACKFILL_PAGE_SIZE", "2")
    monkeypatch.setenv("BACKFILL_RESULT_CAP", "4")
    get_settings.cache_clear()
    reset_token_cache()
    reset_api_state()
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                201, json={"token": "ghs_backfill", "expires_at": "2099-01-01T00:00:00Z"}
            )
        )
        yield router
    get_settings.cache_clear()
    reset_token_cache()
    reset_api_state()


async def seed_repo(session) -> Repository:
    from app.upserts import upsert_installation, upsert_repository

    await upsert_installation(session, installation_id=INSTALLATION_ID)
    await upsert_repository(
        session,
        installation_id=INSTALLATION_ID,
        repository={
            "id": REPO_ID,
            "full_name": FULL_NAME,
            "name": "flakehound",
            "private": True,
            "owner": {"login": "noah-n-pham"},
        },
    )
    await session.flush()
    return await session.get(Repository, REPO_ID)


def run_payload(run_id: int, *, attempt: int = 1, sha: str = "a" * 40, conclusion="success"):
    return {
        "id": run_id,
        "run_attempt": attempt,
        "workflow_id": WORKFLOW_ID,
        "path": ".github/workflows/ci.yml",
        "head_sha": sha,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": conclusion,
        "run_started_at": "2026-08-30T10:00:00Z",
        "created_at": "2026-08-30T10:00:00Z",
        "updated_at": "2026-08-30T10:05:00Z",
    }


def job_payload(job_id: int, run_id: int, *, attempt: int, name: str, conclusion: str):
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "head_sha": "a" * 40,
        "head_branch": "main",
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-08-30T10:00:00Z",
        "completed_at": "2026-08-30T10:04:00Z",
        "runner_name": "GitHub Actions 1",
        "labels": ["ubuntu-latest"],
        "steps": [{"status": "completed"}, {"status": "completed"}],
    }


def runs_response(total: int, runs: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"total_count": total, "workflow_runs": runs},
        headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4999"},
    )


async def advance(session) -> None:
    """One turn of the crawl, the way the worker takes it.

    The queued row is completed before its handler runs, because that is what
    `run_once` does — claim, then handle. Without it every assertion about what
    the crawl queued next would also be counting the row it was given.
    """
    await session.execute(
        update(EventQueue)
        .where(EventQueue.job_type == RUNS_JOB_TYPE, EventQueue.status == "pending")
        .values(status="done")
        .execution_options(synchronize_session=False)
    )
    await handle_backfill_runs(session, {"repo_id": REPO_ID})
    await session.flush()


async def queued(session, job_type: str) -> list[dict]:
    rows = (
        await session.execute(
            select(EventQueue.payload)
            .where(EventQueue.job_type == job_type, EventQueue.status == "pending")
            .order_by(EventQueue.id)
        )
    ).scalars()
    return list(rows)


# --------------------------------------------------------------------------- #
# Starting, and one page
# --------------------------------------------------------------------------- #


async def test_starting_points_the_cursor_at_the_newest_window(db_session, github):
    repo = await seed_repo(db_session)

    queue_id = await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()

    today = datetime.now(UTC).date()
    assert queue_id is not None
    assert repo.backfill_status == "running"
    assert repo.backfill_window_end == today
    assert repo.backfill_window_start == today - timedelta(days=2)
    assert repo.backfill_page == 1
    assert await queued(db_session, RUNS_JOB_TYPE) == [{"repo_id": REPO_ID}]


async def test_a_second_start_does_not_queue_a_second_crawl(db_session, github):
    await seed_repo(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()

    assert await start_backfill(db_session, repo_id=REPO_ID) is None
    await db_session.flush()
    assert len(await queued(db_session, RUNS_JOB_TYPE)) == 1


async def test_a_page_records_its_runs_and_queues_every_attempts_jobs(db_session, github):
    """The listing describes only the latest attempt, and Signal A lives in the
    earlier ones — so a run on attempt 3 owes three jobs fetches, not one."""
    await seed_repo(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    github.get(RUNS_URL).mock(
        return_value=runs_response(2, [run_payload(101), run_payload(102, attempt=3)])
    )

    await advance(db_session)

    runs = (
        await db_session.execute(
            select(WorkflowRun.run_id, WorkflowRun.run_attempt, WorkflowRun.conclusion).order_by(
                WorkflowRun.run_id
            )
        )
    ).all()
    assert runs == [(101, 1, "success"), (102, 3, "success")]
    assert await queued(db_session, JOBS_JOB_TYPE) == [
        {"repo_id": REPO_ID, "run_id": 101, "run_attempt": 1},
        {"repo_id": REPO_ID, "run_id": 102, "run_attempt": 1},
        {"repo_id": REPO_ID, "run_id": 102, "run_attempt": 2},
        {"repo_id": REPO_ID, "run_id": 102, "run_attempt": 3},
    ]


async def test_the_cursor_moves_to_the_next_page_before_the_next_window(db_session, github):
    """Page size is 2 here, so a window of 3 results is not finished in one page."""
    repo = await seed_repo(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    window_end = repo.backfill_window_end
    github.get(RUNS_URL).mock(
        return_value=runs_response(3, [run_payload(101), run_payload(102)])
    )

    await advance(db_session)

    assert repo.backfill_page == 2
    assert repo.backfill_window_end == window_end


async def test_a_finished_window_hands_over_to_the_older_one(db_session, github):
    repo = await seed_repo(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    was_start = repo.backfill_window_start
    github.get(RUNS_URL).mock(return_value=runs_response(1, [run_payload(101)]))

    await advance(db_session)

    assert repo.backfill_page == 1
    assert repo.backfill_window_end == was_start - timedelta(days=1)
    # And the next page is on the queue, because the crawl is not finished.
    assert len(await queued(db_session, RUNS_JOB_TYPE)) == 1


async def test_reaching_the_floor_finishes_the_backfill(db_session, github):
    repo = await seed_repo(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    github.get(RUNS_URL).mock(return_value=runs_response(0, []))

    # Seven days in three-day windows is three windows: 3, 3, 1.
    for _ in range(3):
        await advance(db_session)

    assert repo.backfill_status == "done"
    assert repo.backfill_completed_at is not None
    assert repo.backfill_window_end is None
    # Nothing further was queued: a finished crawl stops rather than spinning.
    assert await queued(db_session, RUNS_JOB_TYPE) == []


async def test_a_window_over_the_result_cap_is_halved_and_retried(db_session, github):
    """The ~1000-result cap is the reason windows exist at all. A window over it
    loses its oldest results with no error, so it must never be paged through."""
    repo = await seed_repo(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    was_end = repo.backfill_window_end
    was_start = repo.backfill_window_start
    github.get(RUNS_URL).mock(
        return_value=runs_response(9, [run_payload(101), run_payload(102)])
    )

    await advance(db_session)

    assert repo.backfill_window_end == was_end
    assert repo.backfill_window_start == was_start + timedelta(days=1)
    assert repo.backfill_page == 1
    # The overflowing page was discarded rather than recorded: it is the newest
    # 2 of 9 and the narrowed window will return them again.
    assert (await db_session.execute(select(func.count()).select_from(WorkflowRun))).scalar() == 0


async def test_a_repo_the_app_cannot_read_fails_instead_of_retrying(db_session, github):
    """404 on the listing means the repo is gone or access was revoked. Five
    retries cannot fix that, and a `failed` cursor is visible."""
    repo = await seed_repo(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    github.get(RUNS_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

    await advance(db_session)

    assert repo.backfill_status == "failed"
    assert await queued(db_session, RUNS_JOB_TYPE) == []


# --------------------------------------------------------------------------- #
# One attempt's jobs
# --------------------------------------------------------------------------- #


async def test_an_earlier_attempts_jobs_are_stored_against_a_stubbed_run(db_session, github):
    """Only the latest attempt has a run row from the listing. The composite
    foreign key means attempt 1 needs one before its jobs can land, and the job
    payload carries everything a stub needs."""
    await seed_repo(db_session)
    github.get(
        f"https://api.github.com/repos/{FULL_NAME}/actions/runs/101/attempts/1/jobs"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "jobs": [
                    job_payload(9001, 101, attempt=1, name="test (3.12)", conclusion="failure")
                ],
            },
        )
    )

    await handle_backfill_jobs(
        db_session, {"repo_id": REPO_ID, "run_id": 101, "run_attempt": 1}
    )
    await db_session.flush()

    assert (
        await db_session.execute(
            select(WorkflowRun.run_id, WorkflowRun.run_attempt)
        )
    ).all() == [(101, 1)]
    assert (
        await db_session.execute(select(Job.id, Job.name, Job.conclusion))
    ).all() == [(9001, "test (3.12)", "failure")]


async def test_backfilled_attempts_produce_a_flake_event(db_session, github):
    """The point of the whole section: history we never saw becomes detection.

    Attempt 1 fails, attempt 2 passes, same run — Signal A, derived from the API
    rather than from a webhook.
    """
    await seed_repo(db_session)
    for attempt, conclusion in ((1, "failure"), (2, "success")):
        github.get(
            f"https://api.github.com/repos/{FULL_NAME}/actions/runs/101/attempts/{attempt}/jobs"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "jobs": [
                        job_payload(
                            9000 + attempt,
                            101,
                            attempt=attempt,
                            name="integration",
                            conclusion=conclusion,
                        )
                    ],
                },
            )
        )
        await handle_backfill_jobs(
            db_session, {"repo_id": REPO_ID, "run_id": 101, "run_attempt": attempt}
        )
        await db_session.flush()

    events = (
        await db_session.execute(select(FlakeEvent.signal, FlakeEvent.job_name))
    ).all()
    assert events == [("rerun_recovery", "integration")]


async def test_a_re_run_older_than_thirty_days_404s_and_that_is_data(db_session, github):
    """SPEC §2's last edge-case row. GitHub discards a re-run's job records after
    about a month while the run itself survives, so the backfill meets this on
    any real 90-day history. It is ordinary, not a failure."""
    await seed_repo(db_session)
    github.get(
        f"https://api.github.com/repos/{FULL_NAME}/actions/runs/101/attempts/1/jobs"
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

    await handle_backfill_jobs(
        db_session, {"repo_id": REPO_ID, "run_id": 101, "run_attempt": 1}
    )
    await db_session.flush()

    assert (await db_session.execute(select(func.count()).select_from(Job))).scalar() == 0
