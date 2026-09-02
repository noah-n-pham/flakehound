"""Operational readouts. Token-gated — "internal" is a name, not a boundary.

The tunnel routes the whole hostname, so `/internal/metrics` is exactly as reachable
from the internet as `/api` is. SPEC §8 is explicit that only `/healthz`,
`/public/flaky`, and the webhook are unauthenticated: queue depth, installation
counts, and rate-limit headroom are not for strangers.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_internal_token
from app.db import session_scope
from app.metrics import latest_snapshot

router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal_token)])

SessionDep = Annotated[AsyncSession, Depends(session_scope)]


class MetricPoint(BaseModel):
    name: str
    value: float
    labels: dict[str, str]


class MetricsResponse(BaseModel):
    """`captured_at` is null only before the worker has written its first sample."""

    captured_at: datetime | None
    age_seconds: float | None
    metrics: list[MetricPoint]


@router.get("/metrics")
async def metrics(session: SessionDep) -> MetricsResponse:
    """The most recent minute's sample of SPEC §9's counters.

    The worker writes the samples and this reads them, so the age is part of the
    answer: a stale `captured_at` means the worker has stopped, which is itself the
    most useful thing this endpoint can tell you.
    """
    captured_at, points = await latest_snapshot(session)
    return MetricsResponse(
        captured_at=captured_at,
        age_seconds=(
            (datetime.now(UTC) - captured_at).total_seconds() if captured_at else None
        ),
        metrics=[
            MetricPoint(name=point.name, value=point.value, labels=point.labels)
            for point in points
        ],
    )
