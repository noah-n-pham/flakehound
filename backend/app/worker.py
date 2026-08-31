"""The worker process: claim, handle, mark, repeat (SPEC §3).

Runs beside the API in the same container. Processing happens here rather than in
the request handler because it calls GitHub's rate-limited API, and coupling
ingest latency to GitHub's response time would turn an upstream slowdown into a
redelivery storm.
"""

import asyncio
import signal

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.handlers import handle
from app.logging import configure_logging, get_logger
from app.queue import claim_batch, mark_done, mark_for_retry

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
            except Exception as exc:
                await session.rollback()
                log.error(
                    "worker.failed",
                    queue_id=job.id,
                    github_event=job.event,
                    attempts=job.attempts,
                    error=str(exc),
                )
                async with sessionmaker() as retry_session:
                    await mark_for_retry(retry_session, job.id, f"{type(exc).__name__}: {exc}")
                    await retry_session.commit()

    return len(claimed)


async def run_forever(stop: asyncio.Event | None = None) -> None:
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    stop = stop or asyncio.Event()
    log.info(
        "worker.started",
        batch_size=settings.worker_batch_size,
        poll_seconds=settings.worker_poll_seconds,
    )

    while not stop.is_set():
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
