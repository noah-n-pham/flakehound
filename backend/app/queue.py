"""Claiming, completing, retrying, and dead-lettering queue rows (SPEC §5)."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Interval, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import EventQueue


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    job_type: str
    event: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int

    @property
    def exhausted(self) -> bool:
        """True when the attempt just claimed is the last one this row will get."""
        return self.attempts >= self.max_attempts


def retry_delay_seconds(attempts: int) -> float:
    """Exponential backoff, capped: the delay before attempt `attempts + 1`.

    Without it the five attempts of a row that fails on a transient condition —
    a Postgres restart, a GitHub 502 — are all spent within one poll interval,
    and work GitHub will never redeliver is dead-lettered a second after the
    outage began.
    """
    settings = get_settings()
    delay = settings.retry_backoff_seconds * 2 ** max(attempts - 1, 0)
    return min(delay, settings.retry_backoff_max_seconds)


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
            or_(EventQueue.next_attempt_at.is_(None), EventQueue.next_attempt_at <= func.now()),
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
            EventQueue.max_attempts,
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


async def mark_for_retry(session: AsyncSession, job: ClaimedJob, error: str) -> bool:
    """Record the failure and decide whether the row lives on.

    Returns True when the row was dead-lettered. The attempt was already counted
    by the claim, so a row whose count has reached the ceiling would never be
    selected again — it goes straight to `failed` rather than sitting in
    `pending` looking like work that is about to happen.
    """
    values: dict[str, Any] = {
        "locked_at": None,
        "last_error": error[:2000],
        "updated_at": func.now(),
    }
    if job.exhausted:
        values["status"] = "failed"
        values["completed_at"] = func.now()
    else:
        values["status"] = "pending"
        # Postgres' clock, not the worker's, so the wait means the same thing to
        # every process that reads the row.
        values["next_attempt_at"] = func.now() + literal(
            timedelta(seconds=retry_delay_seconds(job.attempts)), Interval()
        )

    await session.execute(
        update(EventQueue)
        .where(EventQueue.id == job.id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return job.exhausted


async def reap_stuck(session: AsyncSession) -> list[int]:
    """Return rows claimed longer than the timeout to pending (SPEC §5).

    A worker killed mid-message leaves its row `processing` forever: the claim
    commits before the work starts, precisely so the row is visible as taken,
    which means nothing releases it when the process dies.

    **The timeout must exceed maximum processing time.** Below it the reaper hands
    a second worker a row the first one is still working on, which is not a
    deadlock but duplicated work — survivable only because every handler is
    idempotent, and not something to rely on. The attempt already spent is not
    given back, so a row that reliably kills its worker exhausts its ceiling and
    is dead-lettered rather than being reaped forever.
    """
    timeout = timedelta(seconds=get_settings().reaper_timeout_seconds)
    stuck = (
        update(EventQueue)
        .where(
            EventQueue.status == "processing",
            EventQueue.locked_at < func.now() - literal(timeout, Interval()),
        )
        .values(
            status="pending",
            locked_at=None,
            next_attempt_at=None,
            last_error="reaped: claimed longer than the reaper timeout",
            updated_at=func.now(),
        )
        .returning(EventQueue.id)
        .execution_options(synchronize_session=False)
    )
    return list((await session.execute(stuck)).scalars())


async def fail_exhausted(session: AsyncSession) -> list[int]:
    """The sweep SPEC §5 asks for: mark spent rows failed so they are visible.

    A row can reach the ceiling while still `pending` — the worker died between
    the claim and recording the outcome, so the reaper returned it, or the
    ceiling was lowered under it. Nothing will ever claim it again, and left
    pending it is indistinguishable from work that is merely waiting its turn.
    """
    spent = (
        update(EventQueue)
        .where(
            EventQueue.status == "pending",
            EventQueue.attempts >= EventQueue.max_attempts,
        )
        .values(
            status="failed",
            locked_at=None,
            completed_at=func.now(),
            updated_at=func.now(),
        )
        .returning(EventQueue.id)
        .execution_options(synchronize_session=False)
    )
    return list((await session.execute(spent)).scalars())
