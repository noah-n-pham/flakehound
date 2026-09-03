"""Replaying a delivery 100 times must leave the database as one delivery left it.

This single test is worth more than any architecture diagram: it exercises all
three idempotency layers at once:

* layer 1 — the delivery id is a primary key, so redelivery over HTTP enqueues nothing;
* layer 2 — every fact write is an upsert on GitHub's own ids, so re-processing a payload
  converges instead of duplicating;
* layer 3 — flake events are unique on the grouping key plus the signal, so re-deriving
  the same history cannot mint a second event.

Receipt timestamps and attempt counters legitimately differ between one delivery and a
hundred, so they are excluded from the comparison rather than contorted to match. Note
what is *not* excluded: `jobs.completed_at` is GitHub's fact about the execution, not a
receipt, and a replay that changed it would be a bug.
"""

import json
from random import Random

from sqlalchemy import func, select

from app.models import (
    EventQueue,
    FlakeEvent,
    Installation,
    Job,
    JobStatsDaily,
    Repository,
    WebhookDelivery,
    Workflow,
    WorkflowRun,
)
from app.worker import run_once
from tests import payloads
from tests.helpers import deliver, enqueue, event_for, one_session_factory
from tests.test_detection import ATTEMPT_JOB_IDS, RUN_ID, SHA, WORKFLOW_ID, attempt, run_event
from tests.test_replay_order import JOB_NAME
from tests.test_webhooks import encode, headers

# Receipt bookkeeping, not facts about the CI run.
RECEIPT_COLUMNS = {"created_at", "updated_at", "received_at"}

STATE_TABLES = (
    Installation,
    Repository,
    Workflow,
    WorkflowRun,
    Job,
    FlakeEvent,
    JobStatsDaily,
)


async def snapshot(
    session, *, also_exclude: frozenset[str] = frozenset()
) -> dict[str, list[tuple]]:
    """Every fact and derived row, minus the columns a replay is allowed to move.

    `also_exclude` exists for the backfill's resume test, which compares two
    separate crawls rather than two replays and so has one more receipt to drop.
    Nothing else may use it: every exclusion is a place a bug can hide.
    """
    state = {}
    excluded = RECEIPT_COLUMNS | also_exclude
    for model in STATE_TABLES:
        table = model.__table__
        columns = [c for c in table.columns if c.name not in excluded]
        rows = (
            await session.execute(select(*columns).order_by(*table.primary_key.columns))
        ).all()
        state[table.name] = [tuple(row) for row in rows]
    return state


async def count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def drain(session) -> None:
    factory = one_session_factory(session)
    while await run_once(factory, batch_size=50) > 0:
        pass


async def test_one_delivery_posted_100_times_enqueues_once(client, db_session):
    """Layer 1: GitHub retries a delivery, and the primary key absorbs every retry."""
    body = encode(payloads.workflow_job())

    statuses = set()
    for _ in range(100):
        response = await client.post("/webhooks/github", content=body, headers=headers(body))
        statuses.add((response.status_code, response.json()["status"]))

    assert statuses == {(202, "queued"), (202, "duplicate")}
    assert await count(db_session, WebhookDelivery) == 1
    assert await count(db_session, EventQueue) == 1


async def test_processing_one_payload_100_times_matches_processing_it_once(db_session):
    """Layer 2, in isolation: a hundred queue rows carrying identical bytes."""
    payload = payloads.workflow_job()

    await deliver(db_session, payload)
    once = await snapshot(db_session)

    for _ in range(99):
        enqueue(db_session, payload)
    await db_session.flush()
    await drain(db_session)

    assert await snapshot(db_session) == once
    assert await count(db_session, Job) == 1
    assert await count(db_session, EventQueue) == 100


def full_delivery_set() -> list[dict]:
    """Production's own re-run, as the eighteen deliveries GitHub really sends for it.

    Three attempts of one job, each announced queued, in progress, and completed, and a
    `workflow_run` trio per attempt. Every payload agrees with every other about the facts
    it repeats — a fixture where two bodies disagree about one job's conclusion would make
    the convergence assertion meaningless rather than strict.
    """
    outcomes = (("failure", 14), ("failure", 14), ("success", 18))
    deliveries: list[dict] = []
    for index, (conclusion, steps) in enumerate(outcomes, start=1):
        common = {
            "job_id": ATTEMPT_JOB_IDS[index - 1],
            "run_id": RUN_ID,
            "run_attempt": index,
            "name": JOB_NAME,
            "head_sha": SHA,
        }
        deliveries += [
            payloads.workflow_job(**common, status="queued", conclusion=None, completed_steps=0),
            payloads.workflow_job(
                **common,
                status="in_progress",
                conclusion=None,
                completed_steps=2,
                total_steps=steps,
            ),
            payloads.workflow_job(
                **common, status="completed", conclusion=conclusion, completed_steps=steps
            ),
        ]
        for status, run_conclusion in (
            ("queued", None),
            ("in_progress", None),
            ("completed", conclusion),
        ):
            deliveries.append(
                payloads.workflow_run(
                    run_id=RUN_ID,
                    run_attempt=index,
                    workflow_id=WORKFLOW_ID,
                    head_sha=SHA,
                    status=status,
                    conclusion=run_conclusion,
                )
            )
    return deliveries


async def test_replaying_a_whole_run_100_times_changes_no_fact_and_no_flake_event(db_session):
    """The real shape: three attempts and their run events, replayed out of order.

    The conclusion that a late `in_progress` body would erase is part of the snapshot
    being compared, so out-of-order regression is caught here rather than in the data.
    """
    lifecycle = full_delivery_set()

    await deliver(db_session, *lifecycle)
    once = await snapshot(db_session)

    assert once["jobs"], "the fixture must produce job rows or this test proves nothing"
    assert once["flake_events"], "and a flake event, or layer 3 is never exercised"

    # Deterministic shuffles: replay order must not matter, and a fixed seed keeps a
    # failure reproducible.
    shuffler = Random(20260901)
    for _ in range(100 // len(lifecycle) + 1):
        order = list(lifecycle)
        shuffler.shuffle(order)
        for payload in order:
            enqueue(db_session, payload, event=event_for(payload))
        await db_session.flush()
        await drain(db_session)

    assert await snapshot(db_session) == once


async def test_the_replayed_payloads_were_byte_identical(db_session):
    """Guards the test above: a differing payload would make convergence meaningless."""
    payload = payloads.workflow_job()
    await deliver(db_session, payload, payload, payload)

    bodies = {
        json.dumps(row, sort_keys=True)
        for row in (await db_session.execute(select(EventQueue.payload))).scalars()
    }
    assert len(bodies) == 1


async def test_a_replay_does_not_renumber_a_flake_event(db_session):
    """Layer 3 is an upsert, not a delete and re-insert: the event keeps its identity."""
    lifecycle = (run_event(run_id=RUN_ID), attempt(1, "failure"), attempt(2, "success"))
    await deliver(db_session, *lifecycle)

    before = sorted(
        (await db_session.execute(select(FlakeEvent.id, FlakeEvent.signal))).all()
    )

    for _ in range(10):
        await deliver(db_session, *lifecycle)

    after = sorted((await db_session.execute(select(FlakeEvent.id, FlakeEvent.signal))).all())
    assert after == before
