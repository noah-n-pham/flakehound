"""Walking a repository's Actions history backwards.

The runs listing caps at roughly 1000 results no matter how you page it, so the
history is walked in `created` **date windows** from today backwards, and a
window whose `total_count` overflows the cap is halved until it fits.

Three decisions carry the resumability the roadmap asks for.

**The cursor lives on the repository row, not in the queue payload.** A queue row
is only a wake-up call: it says "advance this repo's backfill", and the handler
reads where the repo got to. So an interrupted backfill resumes wherever it was
even if the queue row that was in flight is reaped, retried, or lost.

**One queue row does exactly one page.** The unit of work stays a single HTTP
call plus a handful of upserts, which keeps it far under the reaper timeout,
lets a live webhook overtake between pages, and makes an interruption cost at
most one page rather than a whole window.

**Advancing the cursor and enqueueing the next page happen in the transaction
that completes the current row.** That is what makes "no gaps and no
duplicates" structural rather than hopeful: the page either happened and the
cursor moved, or neither did.

Installed backfill is `priority = 1`; the observational crawl is `priority = 2`.
`claim_batch` orders by priority, so live events always win, and a real user's
history always wins over the public board. History is not time-sensitive; today's
flake is.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.detection import evaluate_job
from app.github import api_request
from app.logging import get_logger
from app.models import EventQueue, Repository
from app.observe import observation_installation_id
from app.upserts import (
    parse_timestamp,
    run_workflow_id,
    upsert_job,
    upsert_run,
    upsert_workflow,
)

log = get_logger(__name__)

RUNS_JOB_TYPE = "backfill_runs"
JOBS_JOB_TYPE = "backfill_jobs"
# Backfill never competes with a live delivery: lower priority, always.
BACKFILL_PRIORITY = 1
# **The observational crawl is lower still.** It shares one 5,000/hour bucket with a
# real user's history (D-046), so it must lose to both live events and installed
# backfill. Nobody's dashboard may be slow because the public board is filling up.
# `claim_batch` orders by priority, so this is a constant rather than a mechanism.
OBSERVED_PRIORITY = 2
# A matrix wide enough to need more than this many pages of jobs does not exist.
MAX_JOB_PAGES = 10


def is_observed(repo: Repository) -> bool:
    return repo.source == "observed"


def backfill_priority(repo: Repository) -> int:
    return OBSERVED_PRIORITY if is_observed(repo) else BACKFILL_PRIORITY


def backfill_days(repo: Repository) -> int:
    """How far back to walk, which is not the same answer for the two kinds of repo.

    An installed repo gets the full 90 days: its owner asked us to look. An observed
    repo gets the board's own window, because crawling history the public page will
    never show is spending a shared rate limit on nothing.

    Derived from the row rather than stored on it, so the floor here and the first
    window in `start_backfill` cannot drift apart.
    """
    settings = get_settings()
    return settings.observation_backfill_days if is_observed(repo) else settings.backfill_days


def request_identity(repo: Repository) -> int:
    """Whose token reads this repo, and whose bucket pays for it.

    An installed repo uses its own installation. An observed one has no installation
    at all (that is what `source = 'observed'` means), so it borrows the observation
    identity, which is an ordinary installation token that happens to be able to read
    any public repository (D-046).
    """
    if repo.installation_id is not None:
        return repo.installation_id
    return observation_installation_id()


@dataclass(frozen=True)
class Window:
    """A `created` range, inclusive at both ends, as GitHub's filter reads it."""

    start: date
    end: date

    @property
    def span_days(self) -> int:
        return (self.end - self.start).days

    def as_filter(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"

    def halved(self) -> "Window":
        """Keep the newest half. The older half is exactly the next window.

        Narrowing moves `start` forward and leaves `end` alone, and the walk
        derives the next window's `end` from this one's `start` minus a day, so
        halving can never open a gap, whatever it does to the window size.
        """
        return Window(start=self.end - timedelta(days=self.span_days // 2), end=self.end)


def first_window(*, today: date, days: int, window_days: int) -> Window:
    floor = today - timedelta(days=days - 1)
    return Window(start=max(floor, today - timedelta(days=window_days - 1)), end=today)


def next_window(current: Window, *, floor: date, window_days: int) -> Window | None:
    """The window immediately older than this one, or None at the floor."""
    end = current.start - timedelta(days=1)
    if end < floor:
        return None
    return Window(start=max(floor, end - timedelta(days=window_days - 1)), end=end)


# --------------------------------------------------------------------------- #
# Cursor
# --------------------------------------------------------------------------- #


async def _repo(session: AsyncSession, repo_id: int) -> Repository | None:
    return await session.get(Repository, repo_id)


def _save_cursor(
    repo: Repository,
    *,
    window: Window | None,
    page: int | None,
    status: str,
) -> None:
    repo.backfill_window_start = window.start if window else None
    repo.backfill_window_end = window.end if window else None
    repo.backfill_page = page
    repo.backfill_status = status
    if status == "done":
        repo.backfill_completed_at = datetime.now(UTC)


async def _enqueue(
    session: AsyncSession, *, job_type: str, payload: dict[str, Any], priority: int
) -> int:
    row = (
        await session.execute(
            insert(EventQueue)
            .values(job_type=job_type, payload=payload, priority=priority)
            .returning(EventQueue.id)
        )
    ).scalar_one()
    return int(row)


async def _pending_runs_row(session: AsyncSession, repo_id: int) -> int | None:
    """A backfill row already waiting for this repo.

    Two of them would crawl the same cursor twice: harmless, because every
    write is an upsert, but it doubles the API spend for nothing.
    """
    return (
        await session.execute(
            select(EventQueue.id)
            .where(
                EventQueue.job_type == RUNS_JOB_TYPE,
                EventQueue.status.in_(("pending", "processing")),
                EventQueue.payload["repo_id"].astext == str(repo_id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def start_backfill(
    session: AsyncSession, *, repo_id: int, days: int | None = None
) -> int | None:
    """Point the cursor at the newest window and enqueue the first page.

    Returns the queue row's id, or None when a backfill is already under way.
    """
    settings = get_settings()
    repo = await _repo(session, repo_id)
    if repo is None:
        raise ValueError(f"unknown repository {repo_id}")

    existing = await _pending_runs_row(session, repo_id)
    if existing is not None:
        log.info("backfill.already_queued", repo_id=repo_id, queue_id=existing)
        return None

    span = days or backfill_days(repo)
    window = first_window(
        today=datetime.now(UTC).date(),
        days=span,
        window_days=settings.backfill_window_days,
    )
    _save_cursor(repo, window=window, page=1, status="running")
    queue_id = await _enqueue(
        session,
        job_type=RUNS_JOB_TYPE,
        payload={"repo_id": repo_id},
        priority=backfill_priority(repo),
    )
    log.info(
        "backfill.started",
        repo_id=repo_id,
        queue_id=queue_id,
        window=window.as_filter(),
        days=span,
        source=repo.source,
        priority=backfill_priority(repo),
    )
    return queue_id


# --------------------------------------------------------------------------- #
# One page of runs
# --------------------------------------------------------------------------- #


async def handle_backfill_runs(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Fetch one page of one window, record it, and leave the cursor on the next."""
    settings = get_settings()
    repo_id = payload.get("repo_id")
    if not repo_id:
        raise ValueError("backfill_runs payload has no repo_id")

    repo = await _repo(session, repo_id)
    if repo is None:
        raise ValueError(f"unknown repository {repo_id}")
    if repo.backfill_status != "running" or repo.backfill_window_end is None:
        log.info("backfill.not_running", repo_id=repo_id, status=repo.backfill_status)
        return

    window = Window(start=repo.backfill_window_start, end=repo.backfill_window_end)
    page = repo.backfill_page or 1
    floor = datetime.now(UTC).date() - timedelta(days=backfill_days(repo) - 1)
    priority = backfill_priority(repo)

    response = await api_request(
        request_identity(repo),
        "GET",
        f"/repos/{repo.full_name}/actions/runs",
        params={
            "created": window.as_filter(),
            "per_page": settings.backfill_page_size,
            "page": page,
            "exclude_pull_requests": "false",
        },
    )
    if response.status_code == 404:
        # The repo is gone, or the App lost access to it. Not transient, so the
        # queue must not retry it five times.
        _save_cursor(repo, window=None, page=None, status="failed")
        log.warning("backfill.repo_unreadable", repo_id=repo_id, full_name=repo.full_name)
        return
    response.raise_for_status()

    body = response.json()
    total = int(body.get("total_count") or 0)
    runs = body.get("workflow_runs") or []

    # A window wider than the cap loses its oldest results silently, which is the
    # one failure mode of this whole design. Halve it and start again.
    if page == 1 and total > settings.backfill_result_cap and window.span_days >= 1:
        narrowed = window.halved()
        _save_cursor(repo, window=narrowed, page=1, status="running")
        await _enqueue(
            session,
            job_type=RUNS_JOB_TYPE,
            payload={"repo_id": repo_id},
            priority=priority,
        )
        log.info(
            "backfill.window_narrowed",
            repo_id=repo_id,
            was=window.as_filter(),
            now=narrowed.as_filter(),
            total_count=total,
        )
        return
    if page == 1 and total > settings.backfill_result_cap:
        # A single day over the cap cannot be narrowed further. Take what the API
        # will give rather than looping, and say so.
        log.warning(
            "backfill.day_over_cap",
            repo_id=repo_id,
            window=window.as_filter(),
            total_count=total,
            cap=settings.backfill_result_cap,
        )

    enqueued = 0
    for run in runs:
        enqueued += await _record_run(
            session, repo_id=repo_id, run=run, priority=priority
        )

    reachable = min(total, settings.backfill_result_cap)
    more_pages = page * settings.backfill_page_size < reachable and len(runs) > 0
    if more_pages:
        _save_cursor(repo, window=window, page=page + 1, status="running")
    else:
        following = next_window(
            window, floor=floor, window_days=settings.backfill_window_days
        )
        if following is None:
            _save_cursor(repo, window=None, page=None, status="done")
        else:
            _save_cursor(repo, window=following, page=1, status="running")

    if repo.backfill_status == "running":
        await _enqueue(
            session,
            job_type=RUNS_JOB_TYPE,
            payload={"repo_id": repo_id},
            priority=priority,
        )

    log.info(
        "backfill.page",
        repo_id=repo_id,
        window=window.as_filter(),
        page=page,
        total_count=total,
        runs=len(runs),
        attempts_enqueued=enqueued,
        status=repo.backfill_status,
    )


async def _record_run(
    session: AsyncSession, *, repo_id: int, run: dict[str, Any], priority: int
) -> int:
    """Store the run attempt the listing describes, and queue every attempt's jobs.

    The listing returns only the **latest** attempt of each run. Signal A lives
    entirely in the earlier ones, so each attempt from 1 to `run_attempt` gets its
    own queue row; the jobs endpoint is per-attempt and carries what those rows
    need.
    """
    run_id = run.get("id")
    if not run_id:
        return 0

    workflow_id = run.get("workflow_id")
    if workflow_id:
        # The listing carries no workflow object, only the id and the file path.
        # A row must exist before a run may reference it by foreign key; the name
        # arrives later from a webhook and COALESCE keeps it.
        await upsert_workflow(
            session, repo_id=repo_id, workflow={"id": workflow_id, "path": run.get("path")}
        )

    latest_attempt = int(run.get("run_attempt") or 1)
    await upsert_run(
        session,
        repo_id=repo_id,
        run_id=run_id,
        run_attempt=latest_attempt,
        head_sha=run["head_sha"],
        workflow_id=workflow_id,
        head_branch=run.get("head_branch"),
        event=run.get("event"),
        status=run.get("status"),
        conclusion=run.get("conclusion"),
        run_started_at=parse_timestamp(run.get("run_started_at")),
        github_created_at=parse_timestamp(run.get("created_at")),
        github_updated_at=parse_timestamp(run.get("updated_at")),
    )

    for attempt in range(1, latest_attempt + 1):
        await _enqueue(
            session,
            job_type=JOBS_JOB_TYPE,
            payload={"repo_id": repo_id, "run_id": run_id, "run_attempt": attempt},
            priority=priority,
        )
    return latest_attempt


# --------------------------------------------------------------------------- #
# One run attempt's jobs
# --------------------------------------------------------------------------- #


async def handle_backfill_jobs(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Fetch the jobs of one run attempt and run detection over them."""
    settings = get_settings()
    repo_id = payload.get("repo_id")
    run_id = payload.get("run_id")
    run_attempt = payload.get("run_attempt")
    if not repo_id or not run_id or not run_attempt:
        raise ValueError("backfill_jobs payload is incomplete")

    repo = await _repo(session, repo_id)
    if repo is None:
        raise ValueError(f"unknown repository {repo_id}")

    names: set[str] = set()
    page = 1
    while page <= MAX_JOB_PAGES:
        response = await api_request(
            request_identity(repo),
            "GET",
            f"/repos/{repo.full_name}/actions/runs/{run_id}/attempts/{run_attempt}/jobs",
            params={"per_page": settings.backfill_page_size, "page": page},
        )
        if response.status_code == 404:
            # An expected edge case. GitHub discards a re-run's job records
            # after ~30 days while the run itself survives, so this is ordinary
            # for old history and must not fail the row.
            log.info(
                "backfill.jobs_expired",
                repo_id=repo_id,
                run_id=run_id,
                run_attempt=run_attempt,
            )
            return
        response.raise_for_status()

        body = response.json()
        jobs = body.get("jobs") or []
        for job in jobs:
            await _record_job(session, repo_id=repo_id, run_attempt=run_attempt, job=job)
            names.add(job["name"])

        if page * settings.backfill_page_size >= int(body.get("total_count") or 0):
            break
        page += 1

    for name in sorted(names):
        await evaluate_job(session, repo_id=repo_id, run_id=run_id, job_name=name)

    log.info(
        "backfill.jobs",
        repo_id=repo_id,
        run_id=run_id,
        run_attempt=run_attempt,
        jobs=len(names),
    )


async def _record_job(
    session: AsyncSession, *, repo_id: int, run_attempt: int, job: dict[str, Any]
) -> None:
    head_sha = job["head_sha"]
    run_id = job["run_id"]
    # An earlier attempt has no row of its own (the listing only described the
    # latest), so it is stubbed from the job the same way a `workflow_job`
    # webhook stubs its run. The composite foreign key requires it.
    await upsert_run(
        session,
        repo_id=repo_id,
        run_id=run_id,
        run_attempt=run_attempt,
        head_sha=head_sha,
        head_branch=job.get("head_branch"),
    )
    await upsert_job(
        session,
        repo_id=repo_id,
        job={**job, "run_attempt": run_attempt},
        head_sha=head_sha,
        workflow_id=await run_workflow_id(session, run_id=run_id),
    )


async def start_observed_backfills(
    session: AsyncSession, *, limit: int, days: int | None = None
) -> list[int]:
    """Queue the crawl for up to `limit` admitted repositories that have none yet.

    Bounded on purpose. The pool is deliberately larger than the board needs, so the
    crawl expands a few repositories at a time until there are enough genuine rows,
    rather than committing the whole shared rate-limit budget to it in one pass.

    Only `pending` repos are picked up, so running this twice does not restart a crawl
    that is already under way or redo one that finished.
    """
    repo_ids = list(
        (
            await session.execute(
                select(Repository.id)
                .where(
                    Repository.source == "observed",
                    Repository.active.is_(True),
                    Repository.backfill_status == "pending",
                )
                .order_by(Repository.id)
                .limit(limit)
            )
        ).scalars()
    )

    started: list[int] = []
    for repo_id in repo_ids:
        if await start_backfill(session, repo_id=repo_id, days=days) is not None:
            started.append(repo_id)

    log.info("backfill.observed_started", requested=limit, started=len(started))
    return started


async def _main(repo_id: int | None, days: int | None, observed: int | None) -> None:
    from app.db import dispose_engine, get_sessionmaker

    async with get_sessionmaker()() as session:
        if observed:
            await start_observed_backfills(session, limit=observed, days=days)
        else:
            await start_backfill(session, repo_id=repo_id, days=days)
        await session.commit()
    await dispose_engine()


def main() -> None:
    """Queue a backfill; the worker runs it.

    `python -m app.backfill --repo-id N [--days 90]` for one repository, or
    `python -m app.backfill --observed 3` to crawl the next few admitted public repos.
    """
    import argparse
    import asyncio

    from app.logging import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Queue a repository's history backfill.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repo-id", type=int)
    group.add_argument(
        "--observed",
        type=int,
        metavar="N",
        help="start the crawl for N admitted observed repos that have no backfill yet",
    )
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(_main(args.repo_id, args.days, args.observed))


if __name__ == "__main__":
    main()
