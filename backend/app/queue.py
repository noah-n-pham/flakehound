"""Claiming, completing, and returning queue rows (SPEC §5)."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventQueue


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    job_type: str
    event: str | None
    payload: dict[str, Any]
    attempts: int


async def claim_batch(session: AsyncSession, limit: int) -> list[ClaimedJob]:
    """Claim up to `limit` rows in one statement.

    The inner select takes row locks with SKIP LOCKED, so concurrent workers step
    over each other's rows instead of blocking on them and never receive the same
    row. Ordering by priority then age is what makes live webhooks overtake
    backfill work. The attempt count is incremented as part of the claim, so a row
    that keeps killing its worker eventually stops being selected rather than
    looping forever.
    """
    claimable = (
        select(EventQueue.id)
        .where(
            EventQueue.status == "pending",
            EventQueue.attempts < EventQueue.max_attempts,
        )
        .order_by(EventQueue.priority, EventQueue.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claim = (
        update(EventQueue)
        .where(EventQueue.id.in_(claimable))
        .values(
            status="processing",
            locked_at=func.now(),
            attempts=EventQueue.attempts + 1,
            updated_at=func.now(),
        )
        .returning(
            EventQueue.id,
            EventQueue.job_type,
            EventQueue.event,
            EventQueue.payload,
            EventQueue.attempts,
        )
        .execution_options(synchronize_session=False)
    )
    rows = (await session.execute(claim)).all()
    return [ClaimedJob(*row) for row in rows]


async def mark_done(session: AsyncSession, job_id: int) -> None:
    await session.execute(
        update(EventQueue)
        .where(EventQueue.id == job_id)
        .values(status="done", completed_at=func.now(), locked_at=None, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def mark_for_retry(session: AsyncSession, job_id: int, error: str) -> None:
    """Back to pending with the error recorded. The attempt was already counted."""
    await session.execute(
        update(EventQueue)
        .where(EventQueue.id == job_id)
        .values(
            status="pending",
            locked_at=None,
            last_error=error[:2000],
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
