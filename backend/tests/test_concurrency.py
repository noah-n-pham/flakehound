"""Several workers on one queue.

`SKIP LOCKED` is the whole trick: concurrent workers never block each other and
never receive the same row.

Every test here commits for real, because a lock is only observable across
connections, so each one truncates afterwards.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import EventQueue, Job
from app.worker import run_once
from tests import payloads
from tests.helpers import truncate_all

WORKERS = 4
ROWS = 40


@pytest.fixture
async def committing_sessions(database_url: str) -> AsyncIterator[async_sessionmaker]:
    """Sessions on independent connections, whose writes really land.

    NullPool hands every session its own connection, which is what lets one
    session hold a row lock the others must step over.
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


async def _drain(maker: async_sessionmaker, batch_size: int) -> int:
    """One worker's share of the queue: loop until it finds nothing, twice over."""
    processed = 0
    empty_passes = 0
    while empty_passes < 3:
        claimed = await run_once(maker, batch_size)
        processed += claimed
        empty_passes = 0 if claimed else empty_passes + 1
        # Yield, so the workers really interleave rather than draining in turn.
        await asyncio.sleep(0)
    return processed


async def test_four_workers_process_every_row_exactly_once(committing_sessions):
    """The exactly-once claim, counted from the workers' side.

    Every payload names the same repository and the same run, so all four
    workers upsert the same installation, repository, and run rows at the same
    time. Forty unrelated repos would race nothing and prove nothing.
    """
    async with committing_sessions() as setup:
        for i in range(ROWS):
            setup.add(
                EventQueue(
                    job_type="webhook",
                    event="workflow_job",
                    payload=payloads.workflow_job(job_id=90_000_000 + i, name=f"test ({i})"),
                )
            )
        await setup.commit()

    counts = await asyncio.gather(
        *(_drain(committing_sessions, batch_size=3) for _ in range(WORKERS))
    )

    # If any row had been handed to two workers, the totals would exceed the
    # number of rows. This is the assertion that SKIP LOCKED exists for.
    assert sum(counts) == ROWS
    # And every worker did some of it, so the race was real.
    assert min(counts) > 0, counts

    async with committing_sessions() as check:
        statuses = (
            await check.execute(select(EventQueue.status, func.count()).group_by(EventQueue.status))
        ).all()
        assert statuses == [("done", ROWS)]

        attempts = (await check.execute(select(func.max(EventQueue.attempts)))).scalar_one()
        assert attempts == 1

        jobs = (await check.execute(select(func.count()).select_from(Job))).scalar_one()
        assert jobs == ROWS


async def test_a_claim_without_skip_locked_blocks_instead(committing_sessions):
    """Why the clause is there, rather than that the outcome is nice.

    The same claim written without SKIP LOCKED does not return an empty batch.
    It waits for the other worker's transaction, which at one row means the
    second worker stalls behind the first for as long as the first is working.
    """
    async with committing_sessions() as setup:
        setup.add(
            EventQueue(job_type="webhook", event="workflow_job", payload=payloads.workflow_job())
        )
        await setup.commit()

    blocking_claim = (
        update(EventQueue)
        .where(
            EventQueue.id.in_(
                select(EventQueue.id)
                .where(EventQueue.status == "pending")
                .limit(1)
                .with_for_update()  # ← the one difference from claim_batch
            )
        )
        .values(status="processing")
        .returning(EventQueue.id)
        .execution_options(synchronize_session=False)
    )

    async with committing_sessions() as worker_a, committing_sessions() as worker_b:
        # A claims the row and keeps its transaction open, as a worker does
        # while its handler runs.
        assert len((await worker_a.execute(blocking_claim)).all()) == 1

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(worker_b.execute(blocking_claim), timeout=2)

        await worker_b.rollback()
        await worker_a.rollback()
