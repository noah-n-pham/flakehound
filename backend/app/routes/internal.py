"""Operational readouts. Token-gated — "internal" is a name, not a boundary.

The tunnel routes the whole hostname, so `/internal/metrics` is exactly as reachable
from the internet as `/api` is. Exactly three paths are unauthenticated —
`/healthz`, `/public/flaky`, and the webhook — because queue depth, installation
counts, and rate-limit headroom are not for strangers.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_internal_token
from app.backfill import start_observed_backfills
from app.db import session_scope
from app.discover import BANDS, CANDIDATES_PER_BAND
from app.metrics import latest_samples
from app.ops import corpus_counts, enqueue_discovery

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


# --------------------------------------------------------------------------- #
# The control channel (D-058)
#
# The instance takes no inbound connection and there is no shell on it, so this is
# how the build drives the observational crawl now that `aws ecs run-task` is going
# away. Two operations and one readout, all named and typed — nothing here takes a
# query, a command, or anything else that would make it a remote shell.
# --------------------------------------------------------------------------- #


class CorpusResponse(BaseModel):
    """Observed repositories by crawl state, their facts, and the queue draining."""

    observed: dict[str, int]
    observed_facts: dict[str, int]
    queue: dict[str, int]


@router.get("/corpus")
async def corpus(session: SessionDep) -> CorpusResponse:
    """The counts Section I is measured against, straight out of SQL.

    `observed["done"]` is the corpus size — repositories whose crawl finished. Not
    admitted, not installed, and never rounded.
    """
    counts = await corpus_counts(session)
    return CorpusResponse(
        observed=counts.observed,
        observed_facts=counts.observed_facts,
        queue=counts.queue,
    )


class CrawlRequest(BaseModel):
    """`limit` is capped in the schema, not in a docstring.

    The roadmap says to enqueue in bounded tranches and never to dump the whole pool
    on the queue at once. A ceiling in the request model is that rule where it cannot
    be forgotten — the alternative was a sentence in a document and a careful operator.
    """

    limit: int = Field(default=20, ge=1, le=100)
    days: int | None = Field(default=None, ge=1, le=90)


class CrawlResponse(BaseModel):
    started: list[int]
    count: int


@router.post("/ops/observed-crawl")
async def observed_crawl(request: CrawlRequest, session: SessionDep) -> CrawlResponse:
    """Start the crawl for the next `limit` admitted repositories that have none.

    Runs inline rather than through the queue: it is a bounded set of inserts against
    the database this process is already connected to, with no HTTP in it, so it
    answers in the time a request is allowed to take. Only `pending` repositories are
    picked up, so calling it twice does not restart a crawl that is under way.
    """
    started = await start_observed_backfills(session, limit=request.limit, days=request.days)
    await session.commit()
    return CrawlResponse(started=started, count=len(started))


class DiscoverRequest(BaseModel):
    bands: list[str] = Field(default_factory=lambda: [band.name for band in BANDS])
    per_band: int = Field(default=CANDIDATES_PER_BAND, ge=1, le=100)


class DiscoverResponse(BaseModel):
    queued: list[int]
    bands: list[str]


@router.post("/ops/discover")
async def discover_bands(request: DiscoverRequest, session: SessionDep) -> DiscoverResponse:
    """Queue a discovery pass, one row per band; the worker runs them.

    Not inline: a band is around a minute of requests to GitHub and a full pass is
    several, which is longer than Cloudflare will hold an origin request open. The
    result lands in the logs and in `/internal/corpus`, not in this response.
    """
    try:
        queued = await enqueue_discovery(
            session, band_names=tuple(request.bands), per_band=request.per_band
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return DiscoverResponse(queued=queued, bands=request.bands)
