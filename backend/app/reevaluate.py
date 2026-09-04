"""Re-derive detection over deliveries already stored, without calling GitHub.

A signal is applied when a delivery is *processed*, so history ingested before that
signal existed carries no flake events, and nothing will ever redeliver those
webhooks. The payloads are still here, though: every queue row keeps the body it was
enqueued with, so the work can be handed back to the same worker and the same
handlers, and the answer re-derived from the same bytes.

This is not backfill. Backfill asks GitHub for history we never saw and needs an API
client, a rate limiter, and a resumable cursor. This asks nothing of
anyone: it copies stored payloads back onto the queue at backfill priority, so live
webhooks keep overtaking them, and relies on idempotency layers 2 and 3 to make the
re-processing converge rather than duplicate.
"""

import argparse
import asyncio

from sqlalchemy import BigInteger, cast, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import dispose_engine, get_sessionmaker
from app.logging import configure_logging, get_logger
from app.models import EventQueue

log = get_logger(__name__)

# Live webhooks are priority 0. Re-evaluation is history, and history is never urgent.
BACKFILL_PRIORITY = 1

WEBHOOK_JOB_TYPE = "webhook"
REEVALUATE_JOB_TYPE = "reevaluate"


async def enqueue_reevaluation(session: AsyncSession, *, repo_id: int | None = None) -> int:
    """Copy stored webhook payloads back onto the queue. Returns how many were queued.

    The new rows carry no `delivery_id`: the delivery is already recorded and its id is
    a primary key that cannot repeat. That is exactly the case the queue was designed
    to allow: a row with work to do and no inbound delivery behind it.

    Only rows enqueued by the webhook path are copied, so running this twice re-reads
    the original deliveries instead of compounding its own output. The original
    `created_at` comes along, which keeps the deliveries in their true arrival order.
    """
    source = select(
        literal(REEVALUATE_JOB_TYPE),
        EventQueue.event,
        EventQueue.payload,
        literal(BACKFILL_PRIORITY),
        EventQueue.created_at,
    ).where(EventQueue.job_type == WEBHOOK_JOB_TYPE)

    if repo_id is not None:
        # Read the repo out of the payload rather than joining the delivery row: the
        # payload is what this module trusts, and a queue row need not have a delivery.
        source = source.where(
            cast(EventQueue.payload["repository"]["id"].as_string(), BigInteger) == repo_id
        )

    # RETURNING rather than rowcount: psycopg reports -1 for INSERT ... FROM SELECT.
    result = await session.execute(
        insert(EventQueue)
        .from_select(["job_type", "event", "payload", "priority", "created_at"], source)
        .returning(EventQueue.id)
    )
    queued = len(result.all())
    log.info("reevaluate.enqueued", queued=queued, repo_id=repo_id)
    return queued


async def _run(repo_id: int | None) -> None:
    async with get_sessionmaker()() as session:
        await enqueue_reevaluation(session, repo_id=repo_id)
        await session.commit()
    await dispose_engine()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", type=int, default=None, help="limit to one repo")
    args = parser.parse_args()
    asyncio.run(_run(args.repo_id))


if __name__ == "__main__":
    main()
