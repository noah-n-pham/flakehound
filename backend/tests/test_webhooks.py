"""Ingest: signature, atomic enqueue, and dedup by primary key (SPEC §3, §6)."""

import json
from typing import Any

from sqlalchemy import func, select

from app.config import get_settings
from app.models import EventQueue, WebhookDelivery
from app.security import sign_webhook_body
from tests import payloads

DELIVERY_ID = "3f6a1b20-8c9d-4e10-b2a3-000000000001"


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def headers(
    body: bytes,
    *,
    delivery_id: str = DELIVERY_ID,
    event: str = "workflow_job",
    secret: str | None = None,
) -> dict[str, str]:
    secret = secret if secret is not None else get_settings().github_webhook_secret
    return {
        "content-type": "application/json",
        "x-github-delivery": delivery_id,
        "x-github-event": event,
        "x-hub-signature-256": sign_webhook_body(secret, body),
    }


async def count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_healthz_needs_no_auth_and_no_database(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_a_signed_delivery_becomes_a_delivery_row_and_a_queue_row(client, db_session):
    body = encode(payloads.workflow_job())

    response = await client.post("/webhooks/github", content=body, headers=headers(body))

    assert response.status_code == 202
    assert response.json() == {"status": "queued"}

    delivery = (
        await db_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.delivery_id == DELIVERY_ID)
        )
    ).scalar_one()
    assert (delivery.event, delivery.action) == ("workflow_job", "completed")
    assert delivery.repo_id == payloads.REPO_ID
    assert delivery.installation_id == payloads.INSTALLATION_ID

    queued = (
        await db_session.execute(select(EventQueue).where(EventQueue.delivery_id == DELIVERY_ID))
    ).scalar_one()
    assert (queued.status, queued.priority, queued.attempts) == ("pending", 0, 0)
    assert queued.payload["workflow_job"]["id"] == payloads.JOB_ID


async def test_a_forged_signature_is_rejected_and_writes_nothing(client, db_session):
    body = encode(payloads.workflow_job())

    response = await client.post(
        "/webhooks/github", content=body, headers=headers(body, secret="not-the-secret")
    )

    assert response.status_code == 401
    assert await count(db_session, WebhookDelivery) == 0
    assert await count(db_session, EventQueue) == 0


async def test_a_missing_signature_is_rejected(client, db_session):
    body = encode(payloads.workflow_job())
    unsigned = headers(body)
    del unsigned["x-hub-signature-256"]

    response = await client.post("/webhooks/github", content=body, headers=unsigned)

    assert response.status_code == 401
    assert await count(db_session, WebhookDelivery) == 0


async def test_a_signature_over_a_different_body_is_rejected(client, db_session):
    """The HMAC covers the body, so a replayed header with new content must fail."""
    signed_for = encode(payloads.workflow_job(job_id=1))
    tampered = encode(payloads.workflow_job(job_id=2))

    response = await client.post(
        "/webhooks/github", content=tampered, headers=headers(signed_for)
    )

    assert response.status_code == 401
    assert await count(db_session, WebhookDelivery) == 0


async def test_a_redelivered_id_is_accepted_but_enqueues_nothing(client, db_session):
    """Idempotency layer 1. GitHub retries; the primary key absorbs it."""
    body = encode(payloads.workflow_job())

    first = await client.post("/webhooks/github", content=body, headers=headers(body))
    second = await client.post("/webhooks/github", content=body, headers=headers(body))

    assert first.status_code == second.status_code == 202
    assert first.json() == {"status": "queued"}
    assert second.json() == {"status": "duplicate"}
    assert await count(db_session, WebhookDelivery) == 1
    assert await count(db_session, EventQueue) == 1


async def test_a_malformed_body_is_rejected(client, db_session):
    body = b"{not json"

    response = await client.post("/webhooks/github", content=body, headers=headers(body))

    assert response.status_code == 400
    assert await count(db_session, WebhookDelivery) == 0


async def test_a_delivery_without_its_headers_is_rejected(client, db_session):
    body = encode(payloads.workflow_job())
    incomplete = headers(body)
    del incomplete["x-github-event"]

    response = await client.post("/webhooks/github", content=body, headers=incomplete)

    assert response.status_code == 400
    assert await count(db_session, WebhookDelivery) == 0
