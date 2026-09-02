"""The worker process: claim, handle, mark, repeat (SPEC §3).

Runs beside the API in the same container. Processing happens here rather than in
the request handler because it calls GitHub's rate-limited API, and coupling
ingest latency to GitHub's response time would turn an upstream slowdown into a
redelivery storm.
"""

import asyncio
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.handlers import handle
from app.logging import configure_logging, get_logger
from app.queue import (
    claim_batch,
    defer,
    fail_exhausted,
    mark_done,
    mark_for_retry,
    reap_stuck,
)
from app.ratelimit import RateLimitExceeded
from app.rollup import rollup_recent

log = get_logger(__name__)


async def run_once(sessionmaker: async_sessionmaker, batch_size: int) -> int:
    """Claim a batch, then process each row in its own transaction.

    The claim commits before any work starts: a row must be visible as
    `processing` to everyone else while it is being handled, which is also what
    leaves a crashed worker's row claimed for the reaper to find.
    """
    async with sessionmaker() as session:
        claimed = await claim_batch(session, batch_size)
        await session.commit()

    for job in claimed:
        async with sessionmaker() as session:
            try:
                await handle(session, job)
                await mark_done(session, job.id)
                await session.commit()
                log.info("worker.processed", queue_id=job.id, github_event=job.event)
            except RateLimitExceeded as exc:
                # Not a failure: GitHub said "not yet". The row goes back with the
                # wait it was given and keeps every attempt it had, because none
                # of them was spent here.
                await session.rollback()
                async with sessionmaker() as defer_session:
                    await defer(
                        defer_session,
                        job,
                        seconds=exc.retry_after,
                        reason=f"rate limited: {exc}",
                    )
                    await defer_session.commit()
                log.warning(
                    "worker.deferred",
                    queue_id=job.id,
                    installation_id=exc.installation_id,
                    retry_after=round(exc.retry_after, 1),
                    attempts=job.attempts - 1,
                )
            except Exception as exc:
                await session.rollback()
                async with sessionmaker() as retry_session:
                    dead = await mark_for_retry(
                        retry_session, job, f"{type(exc).__name__}: {exc}"
                    )
                    await retry_session.commit()
                log.error(
                    "worker.dead_lettered" if dead else "worker.failed",
                    queue_id=job.id,
                    github_event=job.event,
                    attempts=job.attempts,
                    max_attempts=job.max_attempts,
                    error=str(exc),
                )

    return len(claimed)


@dataclass(frozen=True)
class SweepResult:
    reaped: list[int]
    failed: list[int]
    rolled_up: list[int]


async def sweep(sessionmaker: async_sessionmaker) -> SweepResult:
    """Recover abandoned rows, fail the ones that are out of attempts, roll up stats.

    The first two in that order, and in one transaction: reaping returns a row to
    pending with its spent attempt still counted, so a row whose last attempt died
    with its worker is dead-lettered by the same pass instead of waiting out another
    interval as work that looks pending but can never be claimed.

    The rollup rides along here rather than running per delivery because it is a
    recompute rather than an increment, and recomputing a repo once a minute is
    cheaper than recomputing it once per event. The cost is that a read is up to one
    sweep behind the facts, which for a CI dashboard is not a cost at all.
    """
    settings = get_settings()
    async with sessionmaker() as session:
        reaped = await reap_stuck(session)
        spent = await fail_exhausted(session)
        await session.commit()
    if reaped:
        log.warning("worker.reaped_stuck", queue_ids=reaped, count=len(reaped))
    if spent:
        log.warning("worker.swept_exhausted", queue_ids=spent, count=len(spent))

    # Three intervals of slack, so a pass delayed or skipped by a slow one does not
    # leave a repo's writes unrolled. Overlapping windows only mean a repo is
    # recomputed twice, which is the whole point of the recompute being idempotent.
    since = datetime.now(UTC) - timedelta(seconds=settings.queue_sweep_seconds * 3)
    rolled = await rollup_recent(sessionmaker, since=since)
    if rolled:
        log.info("worker.rolled_up", repo_ids=[r.repo_id for r in rolled], count=len(rolled))

    return SweepResult(reaped=reaped, failed=spent, rolled_up=[r.repo_id for r in rolled])


async def run_forever(stop: asyncio.Event | None = None) -> None:
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    stop = stop or asyncio.Event()
    log.info(
        "worker.started",
        batch_size=settings.worker_batch_size,
        poll_seconds=settings.worker_poll_seconds,
        sweep_seconds=settings.queue_sweep_seconds,
        reaper_timeout_seconds=settings.reaper_timeout_seconds,
    )

    last_sweep = float("-inf")

    while not stop.is_set():
        if time.monotonic() - last_sweep >= settings.queue_sweep_seconds:
            await sweep(sessionmaker)
            last_sweep = time.monotonic()

        processed = await run_once(sessionmaker, settings.worker_batch_size)
        if processed == 0:
            # Nothing to do. At 0.2-0.5 events/s this is the normal state.
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
            except TimeoutError:
                pass

    log.info("worker.stopped")


def main() -> None:
    configure_logging()

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            await run_forever(stop)
        finally:
            await dispose_engine()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
