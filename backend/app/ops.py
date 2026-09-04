"""The control channel: the handful of things an operator has to be able to trigger.

They used to run as `aws ecs run-task` with a command override: a one-off copy of the
image, started by hand, printing into CloudWatch (D-022). ECS is gone and Section I
still needs to drive discovery and crawl tranches, so the capability lives here.

**The endpoint enqueues; the worker works.** A discovery pass is a few minutes of
HTTP against GitHub, and Cloudflare gives up on an origin request long before that,
so a synchronous endpoint would return 524 while the work carried on invisibly behind
it. Postgres is already the queue, and queue rows are already allowed to exist with no
webhook delivery behind them, so an operator command is a queue row like any other and
inherits retries, the reaper, and the rate limiter for free.

**One band per row, not one pass per row.** A three-band pass at 40 candidates a band
measured about four minutes (turn 59), and `reaper_timeout_seconds` is five: a row that
long sits close enough to the reaper's edge that a slow GitHub would have it declared
abandoned mid-crawl. One band is well under a hundred seconds.

Nothing here evaluates a string as SQL or as a command. Each operation is a named,
bounded thing with typed arguments, because this is reachable from the internet with a
bearer token and the difference between "restart the crawl" and "run this query" is the
difference between an operations endpoint and a remote shell.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.discover import BANDS, CANDIDATES_PER_BAND, Band, discover
from app.ingest import LIVE_PRIORITY
from app.logging import get_logger
from app.models import EventQueue, FlakeEvent, Job, Repository, WorkflowRun
from app.observe import observation_installation_id

log = get_logger(__name__)

DISCOVER_JOB_TYPE = "discover_band"

# The same tier as a live delivery, which sounds aggressive and is not: rows inside a
# tier are claimed oldest-first, so a command created now still waits behind every
# webhook already pending. What it does buy is not waiting behind the crawl. There
# are twelve thousand observed backfill rows at priority 2, and a control channel that
# queues behind them is not a control channel.
OPS_PRIORITY = LIVE_PRIORITY


def band_by_name(name: str) -> Band:
    for band in BANDS:
        if band.name == name:
            return band
    known = ", ".join(b.name for b in BANDS)
    raise ValueError(f"unknown band {name!r}; known bands are {known}")


async def enqueue_discovery(
    session: AsyncSession, *, band_names: tuple[str, ...], per_band: int
) -> list[int]:
    """Queue one discovery row per band. Caller commits.

    Band names are resolved here rather than in the handler so that a typo is a 400 on
    the request that made it, not a dead-lettered row found later in a log.
    """
    bands = [band_by_name(name) for name in band_names]
    queued: list[int] = []
    for band in bands:
        row = (
            await session.execute(
                insert(EventQueue)
                .values(
                    job_type=DISCOVER_JOB_TYPE,
                    payload={"band": band.name, "per_band": per_band},
                    priority=OPS_PRIORITY,
                )
                .returning(EventQueue.id)
            )
        ).scalar_one()
        queued.append(int(row))

    log.info(
        "ops.discovery_enqueued",
        bands=[band.name for band in bands],
        per_band=per_band,
        queue_ids=queued,
    )
    return queued


async def handle_discover_band(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Run one band's discovery pass.

    Safe to retry: `discover` skips repositories already stored, so a row that dies
    after committing half a band re-screens nothing it admitted.
    """
    band = band_by_name(payload["band"])
    per_band = int(payload.get("per_band") or CANDIDATES_PER_BAND)

    result = await discover(
        session,
        installation_id=observation_installation_id(),
        bands=(band,),
        per_band=per_band,
    )

    log.info(
        "ops.discover_band",
        band=band.name,
        screened=result.screened,
        skipped=result.skipped,
        admitted=len(result.admitted),
        rejections=result.rejections,
    )


# --------------------------------------------------------------------------- #
# The readout
# --------------------------------------------------------------------------- #


@dataclass
class Corpus:
    """What Section I has to be able to state, counted rather than estimated.

    `observed_done` is the number the roadmap's floor of 335 is measured against, and
    it counts repositories whose crawl finished, not ones admitted, and not GitHub App
    installations, neither of which is the corpus.
    """

    observed: dict[str, int] = field(default_factory=dict)
    observed_facts: dict[str, int] = field(default_factory=dict)
    queue: dict[str, int] = field(default_factory=dict)

    @property
    def observed_done(self) -> int:
        return self.observed.get("done", 0)


async def corpus_counts(session: AsyncSession) -> Corpus:
    """The corpus, its facts, and the queue draining into it, in three queries."""
    observed = {
        status: int(count)
        for status, count in (
            await session.execute(
                select(Repository.backfill_status, func.count())
                .where(Repository.source == "observed")
                .group_by(Repository.backfill_status)
            )
        ).all()
    }
    observed["total"] = sum(observed.values())

    observed_ids = select(Repository.id).where(Repository.source == "observed")
    facts = {}
    for name, model in (
        ("workflow_runs", WorkflowRun),
        ("jobs", Job),
        ("flake_events", FlakeEvent),
    ):
        facts[name] = int(
            await session.scalar(
                select(func.count()).select_from(model).where(model.repo_id.in_(observed_ids))
            )
            or 0
        )

    queue = {
        status: int(count)
        for status, count in (
            await session.execute(
                select(EventQueue.status, func.count()).group_by(EventQueue.status)
            )
        ).all()
    }

    return Corpus(observed=observed, observed_facts=facts, queue=queue)
