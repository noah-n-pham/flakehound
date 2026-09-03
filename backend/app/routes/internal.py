"""Operational readouts. Token-gated — "internal" is a name, not a boundary.

The tunnel routes the whole hostname, so `/internal/metrics` is exactly as reachable
from the internet as `/api` is. Exactly three paths are unauthenticated —
`/healthz`, `/public/flaky`, and the webhook — because queue depth, installation
counts, and rate-limit headroom are not for strangers.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_internal_token
from app.db import session_scope
from app.metrics import latest_samples

router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal_token)])

SessionDep = Annotated[AsyncSession, Depends(session_scope)]


class MetricPoint(BaseModel):
    """One series' newest point. `captured_at` is per point, not per response."""

    name: str
    value: float
    labels: dict[str, str]
    captured_at: datetime


class MetricsResponse(BaseModel):
    """`captured_at` is the newest point of any series, null before the first sample."""

    captured_at: datetime | None
    age_seconds: float | None
    metrics: list[MetricPoint]


@router.get("/metrics")
async def metrics(session: SessionDep) -> MetricsResponse:
    """The newest point of every series still reporting.

    Each point carries its own timestamp because two processes write these counters on
    independent timers — the worker the database-derived ones, the API its own — and a
    single response-level age would hide one of them falling behind. That is the most
    useful thing this endpoint can say: not the numbers, but which writer has stopped.
    """
    now = datetime.now(UTC)
    samples = await latest_samples(session, now=now)
    newest = max((sample.captured_at for sample in samples), default=None)
    return MetricsResponse(
        captured_at=newest,
        age_seconds=(now - newest).total_seconds() if newest else None,
        metrics=[
            MetricPoint(
                name=sample.metric.name,
                value=sample.metric.value,
                labels=sample.metric.labels,
                captured_at=sample.captured_at,
            )
            for sample in samples
        ],
    )
