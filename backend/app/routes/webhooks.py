import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.ingest import record_delivery
from app.logging import get_logger
from app.security import verify_webhook_signature

router = APIRouter()
log = get_logger(__name__)


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def receive_github_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(session_scope)],
) -> dict[str, str]:
    """Verify, record, enqueue, return. Nothing that calls GitHub happens here.

    Authenticated by HMAC signature rather than a bearer token, which is what
    makes it one of the three unauthenticated routes (SPEC §8).
    """
    body = await request.body()
    settings = get_settings()

    if not verify_webhook_signature(
        settings.github_webhook_secret, body, request.headers.get("x-hub-signature-256")
    ):
        log.warning("webhook.signature_invalid", delivery=request.headers.get("x-github-delivery"))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    delivery_id = request.headers.get("x-github-delivery")
    event = request.headers.get("x-github-event")
    if not delivery_id or not event:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing delivery or event header")

    payload: Any = None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload is not a JSON object")

    queued = await record_delivery(
        session, delivery_id=delivery_id, event=event, payload=payload
    )
    # `event` is structlog's own key for the message, hence `github_event`.
    log.info(
        "webhook.received",
        delivery=delivery_id,
        github_event=event,
        action=payload.get("action"),
        duplicate=not queued,
    )
    return {"status": "queued" if queued else "duplicate"}
