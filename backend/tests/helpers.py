"""Shared plumbing: putting work on the queue and running the worker over it."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.models import EventQueue
from app.worker import run_once


async def truncate_all(session: AsyncSession) -> None:
    """Empty every table.

    Only a test that commits for real needs this. The `db_session` fixture rolls
    its work back, but a test that runs concurrent workers cannot — separate
    connections have to see each other's rows — and anything it leaves behind is
    visible to every test that follows.
    """
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    await session.execute(text(f"TRUNCATE {tables} CASCADE"))
    await session.commit()


def enqueue(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    event: str | None = "workflow_job",
    priority: int = 0,
    attempts: int = 0,
    max_attempts: int = 5,
) -> EventQueue:
    row = EventQueue(
        job_type="webhook",
        event=event,
        payload=payload,
        priority=priority,
        attempts=attempts,
        max_attempts=max_attempts,
    )
    session.add(row)
    return row


def event_for(payload: dict[str, Any]) -> str:
    """The event type a payload's shape implies, the way GitHub's header and body agree."""
    for key, event in (
        ("workflow_run", "workflow_run"),
        ("workflow_job", "workflow_job"),
        ("repositories_added", "installation_repositories"),
        ("repositories_removed", "installation_repositories"),
    ):
        if key in payload:
            return event
    return "installation"


def one_session_factory(session: AsyncSession):
    """Hand the worker the test's own session, so its commits still roll back."""

    class _Scope:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc_info):
            return False

    def factory():
        return _Scope()

    return factory


async def deliver(session: AsyncSession, *payloads: dict[str, Any]) -> None:
    """Deliver payloads one at a time, in the order given.

    One at a time because rows inserted in a single transaction share
    `created_at` — `now()` is the transaction's clock — so the dequeue order of a
    batch is not defined. Real deliveries arrive separately anyway, and a signal
    that depends on arrival order is a signal worth testing deliberately.

    The event type comes from the payload's own shape, the way GitHub's header and
    body agree in a real delivery.
    """
    factory = one_session_factory(session)
    for payload in payloads:
        enqueue(session, payload, event=event_for(payload))
        await session.flush()
        await run_once(factory, batch_size=10)
