"""SPEC §9's counters, sampled once a minute into `metrics_snapshots`.

The whole observability story is this table plus the structured logs — no
OpenTelemetry, no Prometheus, no Grafana (SPEC §12). At one service and one worker
that is not a compromise: a counter you can `SELECT` and a log line you can grep
answer every question those systems would, and neither has to be operated.

**A sample is a measurement, not a fact**, which is what makes this table different
from every other one here. Rows are name/value/labels so a new counter never needs a
migration, `captured_at` is truncated to the minute so re-running a pass overwrites
its own sample rather than duplicating it, and samples older than
`metrics_retention_days` are pruned — at roughly twenty series a minute this table
would otherwise out-grow the facts it describes.

Three of SPEC §9's numbers are deliberately not here:

* **Slowest queries** are the structured logs' job, via the `slow_query_ms` hook.
  A slow statement's text and parameters do not fit a numeric series, and the thing
  you want when one shows up is the query, not its rate.
* **API latency per endpoint** lives in the API process, which does not write this
  table — the worker does, so that there is exactly one writer. It arrives as its
  own slice.
* **Monthly AWS spend** is not something the application can read: Cost Explorer is
  not on the allowed service list, and the number is recorded in `docs/STATE.md`
  where the cost decisions already live.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, extract, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import Base
from app.logging import get_logger
from app.models import (
    EventQueue,
    FlakeEvent,
    Installation,
    Job,
    MetricsSnapshot,
    Repository,
    WebhookDelivery,
    WorkflowRun,
)

log = get_logger(__name__)

# Emitted even when they are zero. A series that vanishes when its count reaches
# zero is indistinguishable from a series that was never collected.
QUEUE_STATUSES = ("pending", "processing", "done", "failed")


@dataclass(frozen=True)
class Metric:
    """One point: a name, a number, and the labels that separate its series."""

    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)


async def _count(session: AsyncSession, entity: Any, *where: Any) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(entity).where(*where))).scalar_one()
    )


async def _product_counters(session: AsyncSession) -> list[Metric]:
    """What the product has accumulated: installs, repos, and ingested facts."""
    live = await _count(session, Installation, Installation.deleted_at.is_(None))
    suspended = await _count(session, Installation, Installation.suspended_at.is_not(None))
    metrics = [
        Metric("installations", live),
        Metric("installations_suspended", suspended),
        Metric(
            "repositories_active",
            await _count(session, Repository, Repository.active.is_(True)),
        ),
        # Counted by attempt, because `(run_id, attempt)` is what identifies a run
        # here and a re-run is a second real execution rather than a revision of the
        # first. A count of distinct run ids would hide exactly the events Signal A
        # exists to find.
        Metric("workflow_run_attempts", await _count(session, WorkflowRun)),
        Metric("jobs", await _count(session, Job)),
        Metric("flake_events", await _count(session, FlakeEvent)),
    ]
    by_signal = await session.execute(
        select(FlakeEvent.signal, func.count()).group_by(FlakeEvent.signal)
    )
    # A separate name rather than a labelled variant of `flake_events`, so summing
    # every row of one series can never double-count the total.
    metrics += [
        Metric("flake_events_by_signal", count, {"signal": signal}) for signal, count in by_signal
    ]
    return metrics


async def _queue_health(
    session: AsyncSession, *, now: datetime, throughput_seconds: float
) -> list[Metric]:
    """Queue depth, dead letters, and how fast the worker is draining it."""
    by_status = await session.execute(
        select(EventQueue.status, func.count()).group_by(EventQueue.status)
    )
    depths = dict(by_status.all())
    metrics = [
        Metric("queue_depth", depths.get(status, 0), {"status": status})
        for status in QUEUE_STATUSES
    ]

    done_recently = await _count(
        session,
        EventQueue,
        EventQueue.status == "done",
        EventQueue.completed_at >= now - timedelta(seconds=throughput_seconds),
    )
    metrics.append(
        Metric("worker_throughput_events_per_second", done_recently / throughput_seconds)
    )
    return metrics


async def _ingest_lag(
    session: AsyncSession, *, now: datetime, window_seconds: float
) -> list[Metric]:
    """Delivery received → queue row done, at p50/p95/p99 over a trailing window.

    This is the number that says whether ingest is keeping up, and it is measured
    end to end rather than per stage: `webhook_deliveries.received_at` is written by
    the request that accepted the webhook, `event_queue.completed_at` by the worker
    that finished its work. A backlog shows up here before it shows up anywhere else.

    The window is trailing rather than lifetime because a percentile over all of
    history is a fact about the past that no amount of current slowness can move.
    """
    lag = extract("epoch", EventQueue.completed_at - WebhookDelivery.received_at)
    row = (
        await session.execute(
            select(
                func.percentile_cont(0.5).within_group(lag),
                func.percentile_cont(0.95).within_group(lag),
                func.percentile_cont(0.99).within_group(lag),
                func.count(),
            )
            .select_from(EventQueue)
            .join(WebhookDelivery, WebhookDelivery.delivery_id == EventQueue.delivery_id)
            .where(
                EventQueue.status == "done",
                EventQueue.completed_at.is_not(None),
                EventQueue.completed_at >= now - timedelta(seconds=window_seconds),
            )
        )
    ).one()

    p50, p95, p99, samples = row
    metrics = [Metric("ingest_lag_samples", samples)]
    if samples:
        metrics += [
            Metric("ingest_lag_seconds", float(p50), {"quantile": "p50"}),
            Metric("ingest_lag_seconds", float(p95), {"quantile": "p95"}),
            Metric("ingest_lag_seconds", float(p99), {"quantile": "p99"}),
        ]
    return metrics


async def _table_sizes(session: AsyncSession) -> list[Metric]:
    """Bytes per table, indexes and TOAST included (SPEC §9).

    `text()` rather than a Core statement because the catalogue is not in the model
    metadata; the table names are still bound parameters, not concatenated in.
    """
    rows = await session.execute(
        text(
            "SELECT c.relname AS name, pg_total_relation_size(c.oid) AS bytes "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(:tables)"
        ).bindparams(tables=list(Base.metadata.tables)),
    )
    return [Metric("table_bytes", int(size), {"table": name}) for name, size in rows]


def _rate_limit_headroom() -> list[Metric]:
    """Requests left per installation, as the last GitHub response described it.

    Only installations this process has actually called have a bucket, so the series
    appears when the first request is made rather than being invented at zero. In
    the API process there are none — every GitHub call is the worker's.
    """
    from app.github import get_limiter

    return [
        Metric("github_rate_limit_headroom", headroom, {"installation_id": str(installation_id)})
        for installation_id, headroom in get_limiter().headrooms().items()
    ]


async def collect(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    window_seconds: float | None = None,
    throughput_seconds: float | None = None,
) -> list[Metric]:
    """Every counter SPEC §9 asks this process for, measured now."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    window_seconds = window_seconds or settings.metrics_window_seconds
    throughput_seconds = throughput_seconds or settings.metrics_interval_seconds

    return [
        *await _product_counters(session),
        *await _queue_health(session, now=now, throughput_seconds=throughput_seconds),
        *await _ingest_lag(session, now=now, window_seconds=window_seconds),
        *await _table_sizes(session),
        *_rate_limit_headroom(),
    ]


async def store_samples(
    session: AsyncSession, metrics: list[Metric], *, now: datetime
) -> datetime:
    """Write one process's sample of the minute containing `now`. Caller commits.

    `captured_at` is truncated to the minute and the insert upserts, so two passes
    inside one minute leave one sample rather than two — the same discipline every
    other write path here follows, for the same reason: something will eventually
    run twice.

    **Two processes write this table, and that is safe because they write disjoint
    series.** The worker owns everything measurable from the database; the API owns
    the counters only it can see (`app/apimetrics.py`). The unique key is
    `(captured_at, name, labels)`, so the rule is one writer per *series*, not one
    writer per table — nothing here would stop a second writer from fighting over one
    name, so a new series has to belong to exactly one process by construction.
    """
    captured_at = now.replace(second=0, microsecond=0)
    if not metrics:
        return captured_at

    stmt = insert(MetricsSnapshot).values(
        [
            {
                "captured_at": captured_at,
                "name": metric.name,
                "value": metric.value,
                "labels": metric.labels,
            }
            for metric in metrics
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_metrics_snapshots_point", set_={"value": stmt.excluded.value}
        )
    )
    return captured_at


async def write_snapshot(
    session: AsyncSession, *, now: datetime | None = None, **kwargs: Any
) -> list[Metric]:
    """The worker's pass: sample every database-derived counter, then prune old rows.

    Pruning belongs here rather than in `store_samples` so that there is exactly one
    process deleting, whatever number of processes are writing.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)

    metrics = await collect(session, now=now, **kwargs)
    await store_samples(session, metrics, now=now)

    pruned = (
        await session.execute(
            delete(MetricsSnapshot).where(
                MetricsSnapshot.captured_at < now - timedelta(days=settings.metrics_retention_days)
            )
        )
    ).rowcount
    if pruned:
        log.info("metrics.pruned", rows=pruned)
    return metrics


@dataclass(frozen=True)
class Sample:
    """One series' most recent point, and when it was taken."""

    metric: Metric
    captured_at: datetime


async def latest_samples(
    session: AsyncSession, *, now: datetime | None = None, lookback_seconds: float | None = None
) -> list[Sample]:
    """The newest point of every series still reporting — what `/internal/metrics` serves.

    **Per series rather than per minute**, because the two writers run on independent
    timers: the worker's minute and the API's minute usually coincide and sometimes do
    not, and "the latest minute" would drop whichever process happened to write second
    into the previous one. Each point therefore carries its own `captured_at`, and a
    reader can see that one writer has gone quiet while the other has not.

    A series whose writer has stopped for longer than the lookback disappears rather
    than being reported as current. That is the same choice as emitting a zero: a
    number with no time attached is worse than an absence.

    The endpoint reads this table rather than measuring on demand, because half of
    these counters only exist inside the process that produced them.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    lookback = lookback_seconds or settings.metrics_interval_seconds * 5

    rows = (
        (
            await session.execute(
                select(MetricsSnapshot)
                .where(MetricsSnapshot.captured_at >= now - timedelta(seconds=lookback))
                # DISTINCT ON keeps the first row of each group, so the ordering is the
                # selection: newest sample per (name, labels).
                .distinct(MetricsSnapshot.name, MetricsSnapshot.labels)
                .order_by(
                    MetricsSnapshot.name,
                    MetricsSnapshot.labels,
                    MetricsSnapshot.captured_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Sample(Metric(row.name, float(row.value), row.labels), row.captured_at) for row in rows
    ]
