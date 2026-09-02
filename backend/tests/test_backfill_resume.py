"""Interrupting a backfill and restarting it produces no gaps and no duplicates.

The comparison is against an *uninterrupted* crawl of the same fake history, so
the assertion is "identical", not "plausible". Two things make that hold, and
each has a test that isolates it: the cursor lives on the repository row rather
than in the queue payload, and a page advances the cursor in the same
transaction that completes its queue row.

The interruption is the real one — `claim_batch` takes the rows and nothing ever
processes them, which is exactly what a killed worker leaves behind — and the
reaper is what brings them back.
"""

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import Interval, func, literal, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import backfill as backfill_module
from app.backfill import RUNS_JOB_TYPE, start_backfill
from app.config import get_settings
from app.github import reset_api_state, reset_token_cache
from app.models import EventQueue, Job, Repository, WorkflowRun
from app.queue import claim_batch, reap_stuck
from app.worker import run_once
from tests.helpers import truncate_all
from tests.test_backfill import (
    FULL_NAME,
    REPO_ID,
    TOKEN_URL,
    keypair,  # noqa: F401 — a fixture, used by name
    seed_repo,
)
from tests.test_replay_100x import drain, snapshot

RUNS_URL = f"https://api.github.com/repos/{FULL_NAME}/actions/runs"
JOBS_PATH = re.compile(r"/actions/runs/(\d+)/attempts/(\d+)/jobs$")


# --------------------------------------------------------------------------- #
# A fake repository history, spread across every window and page boundary
# --------------------------------------------------------------------------- #


def history(today) -> list[dict]:
    """Six runs over seven days: two pages in one window, a re-run in another.

    Sized against the test settings (7 days of history, 3-day windows, 2 results
    a page) so an interrupt has somewhere to land — three windows, one of which
    needs a second page, and a run with two attempts so a flake event exists to
    be compared.
    """
    spec = [
        (501, 0, 1),
        (502, 1, 1),
        (503, 2, 1),  # window 1 holds three runs, so it needs two pages
        (504, 3, 2),  # a re-run, in window 2
        (505, 4, 1),
        (506, 6, 1),  # window 3, a single day
    ]
    return [
        {
            "id": run_id,
            "run_attempt": attempts,
            "workflow_id": 900_001,
            "path": ".github/workflows/ci.yml",
            "head_sha": f"{run_id:040d}",
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "success" if attempts == 1 else "success",
            "run_started_at": f"{today - timedelta(days=age):%Y-%m-%d}T10:00:00Z",
            "created_at": f"{today - timedelta(days=age):%Y-%m-%d}T10:00:00Z",
            "updated_at": f"{today - timedelta(days=age):%Y-%m-%d}T10:30:00Z",
            "_day": today - timedelta(days=age),
        }
        for run_id, age, attempts in spec
    ]


def jobs_for(run_id: int, attempt: int, attempts: int) -> list[dict]:
    # A re-run's first attempt failed and its second passed: Signal A, so the
    # snapshot being compared includes derived state and not only facts.
    conclusion = "failure" if attempts > 1 and attempt == 1 else "success"
    return [
        {
            "id": run_id * 100 + attempt,
            "run_id": run_id,
            "run_attempt": attempt,
            "head_sha": f"{run_id:040d}",
            "head_branch": "main",
            "name": "integration",
            "status": "completed",
            "conclusion": conclusion,
            "started_at": "2026-08-30T10:00:00Z",
            "completed_at": "2026-08-30T10:20:00Z",
            "runner_name": "GitHub Actions 1",
            "labels": ["ubuntu-latest"],
            "steps": [{"status": "completed"}, {"status": "completed"}],
        }
    ]


@pytest.fixture
def fake_github(monkeypatch, keypair):  # noqa: F811 — the imported fixture
    monkeypatch.setenv("GITHUB_APP_ID", "4792446")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", keypair)
    monkeypatch.setenv("BACKFILL_DAYS", "7")
    monkeypatch.setenv("BACKFILL_WINDOW_DAYS", "3")
    monkeypatch.setenv("BACKFILL_PAGE_SIZE", "2")
    monkeypatch.setenv("BACKFILL_RESULT_CAP", "1000")
    get_settings.cache_clear()
    reset_token_cache()
    reset_api_state()

    today = datetime.now(UTC).date()
    corpus = history(today)
    by_id = {run["id"]: run for run in corpus}
    calls: list[str] = []

    # Set to a 1-based call number to make that runs request blow up once, which
    # is how a worker is killed *inside* a page rather than at the claim.
    fail_at: dict[str, int | None] = {"call": None}

    def list_runs(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        runs_calls = sum(1 for c in calls if "/actions/runs?" in c)
        if fail_at["call"] == runs_calls:
            fail_at["call"] = None
            raise httpx.ConnectError("connection reset", request=request)
        start, _, end = request.url.params["created"].partition("..")
        page = int(request.url.params["page"])
        size = int(request.url.params["per_page"])
        window = [
            run
            for run in corpus
            if start <= run["_day"].isoformat() <= end
        ]
        page_of = window[(page - 1) * size : page * size]
        return httpx.Response(
            200,
            json={
                "total_count": len(window),
                "workflow_runs": [
                    {k: v for k, v in run.items() if k != "_day"} for run in page_of
                ],
            },
        )

    def list_jobs(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        run_id, attempt = (int(g) for g in JOBS_PATH.search(request.url.path).groups())
        jobs = jobs_for(run_id, attempt, by_id[run_id]["run_attempt"])
        return httpx.Response(200, json={"total_count": len(jobs), "jobs": jobs})

    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                201, json={"token": "ghs_resume", "expires_at": "2099-01-01T00:00:00Z"}
            )
        )
        router.get(url__regex=rf"{re.escape(RUNS_URL)}\?.*").mock(side_effect=list_runs)
        router.get(url__regex=r".*/attempts/\d+/jobs.*").mock(side_effect=list_jobs)
        router.calls_made = calls
        router.fail_at = fail_at
        yield router

    get_settings.cache_clear()
    reset_token_cache()
    reset_api_state()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def abandon_everything_claimed(session) -> int:
    """A worker killed mid-message: rows claimed, nothing done, process gone.

    `claim_batch` commits the claim before any work starts, which is precisely
    why a dead worker leaves rows stranded in `processing`. Backdating
    `locked_at` and running the reaper is the recovery path the system already
    has, not a shortcut around it.
    """
    claimed = await claim_batch(session, 50)
    await session.flush()
    if not claimed:
        return 0
    await session.execute(
        update(EventQueue)
        .where(EventQueue.status == "processing")
        .values(locked_at=func.now() - literal(timedelta(minutes=10), Interval()))
        .execution_options(synchronize_session=False)
    )
    reaped = await reap_stuck(session)
    await session.flush()
    return len(reaped)


async def failed_rows(session) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(EventQueue).where(EventQueue.status == "failed")
        )
    ).scalar_one()


async def pending(session) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(EventQueue).where(EventQueue.status == "pending")
        )
    ).scalar_one()


async def run_ids(session) -> list[int]:
    return sorted(
        (await session.execute(select(WorkflowRun.run_id).distinct())).scalars()
    )


# `backfill_completed_at` records when a crawl finished, so two crawls differ by
# construction. It is the only column this comparison drops beyond the replay
# test's receipts — everything else, cursor state included, must match exactly.
CRAWL_RECEIPTS = frozenset({"backfill_completed_at"})


async def crawl_snapshot(session) -> dict[str, list[tuple]]:
    return await snapshot(session, also_exclude=CRAWL_RECEIPTS)


async def clear(session) -> None:
    """Empty the fact tables and the queue, and rewind the cursor.

    The repository and installation rows stay, because the second crawl is the
    same repository being backfilled again, not a different one.

    `RESTART IDENTITY` matters: `flake_events.id` comes from a sequence, so
    without it the second crawl's events are numbered 3 and 4 against the
    first's 1 and 2 and the comparison fails for a reason that has nothing to do
    with resuming. Resetting the sequence keeps the surrogate ids *in* the
    comparison rather than excusing them.
    """
    await session.execute(
        text("TRUNCATE flake_events, jobs, workflow_runs, event_queue RESTART IDENTITY CASCADE")
    )
    await session.execute(
        update(Repository)
        .where(Repository.id == REPO_ID)
        .values(
            backfill_status="pending",
            backfill_window_start=None,
            backfill_window_end=None,
            backfill_page=None,
            backfill_completed_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    # Every queue write is a Core UPDATE with synchronize_session=False, and the
    # repository row was just changed underneath the identity map.
    session.expire_all()


# --------------------------------------------------------------------------- #
# The criterion
# --------------------------------------------------------------------------- #


async def test_a_crawl_interrupted_midway_resumes_to_the_identical_database(
    db_session, fake_github
):
    await seed_repo(db_session)
    # Also before the first crawl: sequences are not transactional in Postgres,
    # so `flake_events.id` carries whatever earlier tests in this session used.
    await clear(db_session)

    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    await drain(db_session)
    uninterrupted = await crawl_snapshot(db_session)

    # The comparison is worthless unless the crawl actually produced something.
    assert len(uninterrupted["workflow_runs"]) == 7, "six runs, one of them a re-run"
    assert uninterrupted["flake_events"], "and a flake event, or only facts are compared"

    await clear(db_session)

    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    # Two turns of the worker, then the process dies holding whatever it claimed.
    await drain_rounds(db_session, 2)
    interrupted_at = await pending(db_session)
    reaped = await abandon_everything_claimed(db_session)
    await drain(db_session)

    # Positive assertions about the interruption itself: a test that resumed
    # nothing would otherwise pass.
    assert interrupted_at > 0, "the crawl must be unfinished when it is interrupted"
    assert reaped > 0, "and rows must actually have been stranded and recovered"
    assert await crawl_snapshot(db_session) == uninterrupted


async def test_a_crawl_interrupted_after_every_single_row_still_converges(
    db_session, fake_github
):
    """The pathological case: every row is claimed, abandoned, reaped, redone.

    Once each, not repeatedly. `claim_batch` counts the attempt, so a row
    abandoned `max_attempts` times stops being claimable and is dead-lettered —
    which is turn 22's deliberate design, not a resume bug. The assertion that
    nothing reached `failed` is what keeps this test honest about the difference.
    """
    await seed_repo(db_session)
    await clear(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    await drain(db_session)
    uninterrupted = await crawl_snapshot(db_session)

    await clear(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()

    interruptions = 0
    for _ in range(60):
        # Process a round first, then strand whatever that round enqueued, so a
        # row is interrupted once rather than until its attempts run out.
        await drain_rounds(db_session, 1)
        if await pending(db_session) == 0:
            break
        interruptions += await abandon_everything_claimed(db_session)
    await drain(db_session)

    assert interruptions >= 5, "every round's new work must really have been stranded"
    assert await failed_rows(db_session) == 0, "one interruption must not dead-letter a row"
    assert await crawl_snapshot(db_session) == uninterrupted


async def test_the_cursor_resumes_the_crawl_even_when_the_queue_is_lost(
    db_session, fake_github
):
    """The design's headline claim, isolated.

    The queue row is a wake-up call, not the state. Throwing away the crawl's
    row mid-window and inserting a bare replacement must pick up exactly where
    the repository row says it got to — no earlier, which would redo a window,
    and no later, which would lose one.

    Only the `backfill_runs` rows are discarded. The cursor tracks the runs
    crawl and nothing else, so a pending `backfill_jobs` row is the only record
    that an attempt's jobs are still owed — see the note in STATE.md Invariants.
    """
    await seed_repo(db_session)
    await clear(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    await drain(db_session)
    uninterrupted = await crawl_snapshot(db_session)

    await clear(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    await drain_rounds(db_session, 2)

    repo = await seed_repo(db_session)
    assert repo.backfill_status == "running", "still mid-crawl, or nothing is being resumed"
    partial = await run_ids(db_session)
    assert 0 < len(partial) < 6, "some history fetched, but not all of it"

    await db_session.execute(
        EventQueue.__table__.delete().where(EventQueue.job_type == RUNS_JOB_TYPE)
    )
    db_session.add(EventQueue(job_type=RUNS_JOB_TYPE, payload={"repo_id": REPO_ID}, priority=1))
    await db_session.flush()
    await drain(db_session)

    assert await run_ids(db_session) == [501, 502, 503, 504, 505, 506]
    assert await crawl_snapshot(db_session) == uninterrupted


async def test_an_interruption_at_the_claim_boundary_costs_no_extra_requests(
    db_session, fake_github
):
    """What resuming costs, measured rather than assumed.

    I expected a stranded row to cost a repeated request, and it does not — the
    counts come out equal. That is the claim-before-work ordering paying off: a
    row is `processing` before any HTTP happens, and the call, the writes, the
    cursor and the completion all commit together. So a worker killed while
    holding a row it has not started spends nothing, and one killed after the
    call repeats exactly that one call and no more.
    """
    await seed_repo(db_session)
    await clear(db_session)
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    await drain(db_session)
    clean_requests = len(fake_github.calls_made)
    clean_job_facts = (
        await db_session.execute(select(func.count()).select_from(Job))
    ).scalar_one()

    await clear(db_session)
    fake_github.calls_made.clear()
    await start_backfill(db_session, repo_id=REPO_ID)
    await db_session.flush()
    await drain_rounds(db_session, 2)
    assert await abandon_everything_claimed(db_session) > 0
    await drain(db_session)

    assert clean_requests == 11, "four pages of runs and seven attempts of jobs"
    assert len(fake_github.calls_made) == clean_requests
    assert (
        await db_session.execute(select(func.count()).select_from(Job))
    ).scalar_one() == clean_job_facts


# --------------------------------------------------------------------------- #
# Dying inside a page, not at the claim boundary
# --------------------------------------------------------------------------- #


@pytest.fixture
async def committing_sessions(database_url):
    """Sessions whose writes really land, on their own connections.

    The tests above share the rolled-back test session, which cannot exercise
    the worker's failure path: `run_once` calls `session.rollback()`, and on a
    savepoint-joined session that would discard the test's own setup. A real
    commit boundary is the only way to see what a mid-page death leaves behind.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as setup:
        await truncate_all(setup)
    try:
        yield maker
    finally:
        async with maker() as cleanup:
            await truncate_all(cleanup)
        await engine.dispose()


async def crawl(maker) -> None:
    for _ in range(200):
        if await run_once(maker, 50) == 0:
            return
    raise AssertionError("the crawl did not finish")


@pytest.mark.parametrize("fault", ["the http call", "writing the page"])
async def test_a_worker_killed_inside_a_page_loses_none_of_it(
    committing_sessions, fake_github, monkeypatch, fault
):
    """The other way a worker dies, and the one the claim boundary cannot show.

    A page that fails halfway must take its cursor advance down with it. If the
    cursor were durable while the page's runs were not, the retry would start
    from the *next* page and those runs would never be fetched by anything — a
    silent gap, which is exactly what this criterion forbids.

    Two fault points, because they are not the same test. A failure in the HTTP
    call happens before the cursor is touched at all, so it proves the retry
    converges but says nothing about ordering. A failure while *writing* the
    page lands in the window between advancing the cursor and recording the
    runs — the window that only exists if those two are not one transaction.
    """
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "0")
    get_settings.cache_clear()

    async with committing_sessions() as session:
        await seed_repo(session)
        await clear(session)
        await start_backfill(session, repo_id=REPO_ID)
        await session.commit()
    await crawl(committing_sessions)
    async with committing_sessions() as session:
        uninterrupted = await crawl_snapshot(session)
    assert len(uninterrupted["workflow_runs"]) == 7

    async with committing_sessions() as session:
        await clear(session)
        await session.commit()
        await seed_repo(session)
        await start_backfill(session, repo_id=REPO_ID)
        await session.commit()

    fired = {"yes": False}
    if fault == "the http call":
        # Counted from here, not from the baseline crawl's requests. The second
        # runs request is page 2 of the first window — mid-crawl, with a page
        # recorded behind it and two windows still to come.
        fake_github.calls_made.clear()
        fake_github.fail_at["call"] = 2
        fired = fake_github.fail_at
    else:
        real_upsert_run = backfill_module.upsert_run

        async def failing_upsert_run(*args, **kwargs):
            if not fired["yes"]:
                fired["yes"] = True
                raise RuntimeError("the process died writing this page")
            return await real_upsert_run(*args, **kwargs)

        monkeypatch.setattr(backfill_module, "upsert_run", failing_upsert_run)

    await crawl(committing_sessions)

    async with committing_sessions() as session:
        assert fired.get("yes") or fired.get("call") is None, "the fault never fired"
        rows = (
            await session.execute(
                select(EventQueue.attempts).where(EventQueue.attempts > 1)
            )
        ).scalars()
        assert list(rows), "a row must have been retried, or nothing was interrupted"
        assert await crawl_snapshot(session) == uninterrupted

    get_settings.cache_clear()


async def queue_count(session, job_type: str) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(EventQueue).where(EventQueue.job_type == job_type)
        )
    ).scalar_one()


async def drain_rounds(session, rounds: int) -> None:
    from app.worker import run_once
    from tests.helpers import one_session_factory

    factory = one_session_factory(session)
    for _ in range(rounds):
        await run_once(factory, batch_size=50)
