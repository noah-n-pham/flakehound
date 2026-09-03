"""Recording an inbound delivery and queuing its work — atomically."""

from typing import Any

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventQueue, WebhookDelivery

LIVE_PRIORITY = 0


def _nested_id(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return value.get("id") if isinstance(value, dict) else None


async def record_delivery(
    session: AsyncSession,
    *,
    delivery_id: str,
    event: str,
    payload: dict[str, Any],
) -> bool:
    """Insert the delivery and its queue row in one transaction.

    Returns False if GitHub has delivered this id before. Dedup is the primary
    key: the duplicate insert raises, we catch it, and nothing is enqueued. That
    constraint is the whole mechanism — nothing outside Postgres participates.
    """
    action = payload.get("action")
    try:
        async with session.begin_nested():
            await session.execute(
                insert(WebhookDelivery).values(
                    delivery_id=delivery_id,
                    event=event,
                    action=action if isinstance(action, str) else None,
                    installation_id=_nested_id(payload, "installation"),
                    repo_id=_nested_id(payload, "repository"),
                )
            )
            await session.execute(
                insert(EventQueue).values(
                    delivery_id=delivery_id,
                    job_type="webhook",
                    event=event,
                    payload=payload,
                    priority=LIVE_PRIORITY,
                )
            )
    except IntegrityError:
        await session.rollback()
        return False

    await session.commit()
    return True
