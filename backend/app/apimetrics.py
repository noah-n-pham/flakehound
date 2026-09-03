"""The counters only the API process can see.

Two of the numbers worth reporting cannot be computed from the database. **API
latency per endpoint** is a property of a request that has already been answered, and
**the duplicate-delivery rate** has no row to count: a duplicate delivery is one whose
insert *failed*, which is precisely how dedup works, so the database's only
record of it is the row that was already there.

So the API keeps them in memory for a minute and writes them into the same
`metrics_snapshots` table the worker writes. The safety rule is one writer per
*series*, not one writer per table — see `metrics.store_samples`.

**Latency is labelled with the route template, never the request path.** A label of
`/api/repos/1352471967/flaky` would create one series per repository and a new one
every time a repository was added; `/api/repos/{repo_id}/flaky` is the thing whose
latency anyone actually wants to know. Unmatched paths — 404s, and anything a scanner
throws at the tunnel — collapse into one label for the same reason.

Samples are kept in a **reservoir**, so memory is bounded whatever the traffic is
while every request in the minute keeps an equal chance of being in the percentile.
An unbounded list would be a memory leak the first time this was load-tested, and a
"keep the first N" cap would quietly report the quietest part of the minute.
"""

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.metrics import Metric, store_samples

UNMATCHED = "<unmatched>"

DELIVERIES_RECEIVED = "webhook_deliveries_received"
DELIVERIES_DUPLICATE = "webhook_deliveries_duplicate"

QUANTILES = (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))


def quantile(samples: list[float], q: float) -> float:
    """The same definition Postgres' `percentile_cont` uses, so the two agree.

    Linear interpolation between the two samples straddling position `q * (n - 1)`.
    Sharing one definition with `metrics.py`'s SQL matters: ingest lag and API latency
    are read side by side and a reader is entitled to assume p95 means one thing.
    """
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("no samples")
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (position - low) * (ordered[high] - ordered[low])


@dataclass
class Reservoir:
    """A bounded, unbiased sample of an unbounded stream (Vitter's algorithm R)."""

    limit: int
    seen: int = 0
    samples: list[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        self.seen += 1
        if len(self.samples) < self.limit:
            self.samples.append(value)
            return
        # Each of the `seen` observations ends up in the reservoir with probability
        # limit/seen, however long the minute turns out to be.
        index = random.randrange(self.seen)
        if index < self.limit:
            self.samples[index] = value


class Recorder:
    """One minute of in-process counters, drained into a sample and reset.

    Not thread-safe and it does not need to be: one uvicorn worker, one event loop,
    and every mutation here is a handful of non-awaiting statements.
    """

    def __init__(self, sample_limit: int = 512) -> None:
        self._sample_limit = sample_limit
        self._latency: dict[str, Reservoir] = {}
        self._counters: dict[str, int] = {}

    def observe_request(self, endpoint: str, seconds: float) -> None:
        reservoir = self._latency.get(endpoint)
        if reservoir is None:
            reservoir = Reservoir(limit=self._sample_limit)
            self._latency[endpoint] = reservoir
        reservoir.add(seconds)

    def count(self, name: str, amount: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + amount

    def drain(self) -> list[Metric]:
        """Everything measured since the last drain, as metrics. Resets the state.

        Drain-and-reset rather than a rolling window, so each sample describes its own
        minute: a p99 that includes the last hour cannot be moved by the present.
        """
        latency, counters = self._latency, self._counters
        self._latency, self._counters = {}, {}

        received = counters.get(DELIVERIES_RECEIVED, 0)
        duplicates = counters.get(DELIVERIES_DUPLICATE, 0)
        metrics = [
            # Always emitted, so an idle minute is distinguishable from a minute
            # nobody collected. The per-endpoint series below cannot be: there is no
            # list of endpoints that were not called.
            Metric("api_requests", sum(r.seen for r in latency.values())),
            Metric(DELIVERIES_RECEIVED, received),
            Metric(DELIVERIES_DUPLICATE, duplicates),
            Metric("duplicate_delivery_rate", duplicates / received if received else 0.0),
        ]
        for endpoint, reservoir in sorted(latency.items()):
            metrics.append(
                Metric("api_requests_by_endpoint", reservoir.seen, {"endpoint": endpoint})
            )
            metrics += [
                Metric(
                    "api_request_seconds",
                    quantile(reservoir.samples, q),
                    {"endpoint": endpoint, "quantile": name},
                )
                for name, q in QUANTILES
            ]
        return metrics


_recorder: Recorder | None = None


def get_recorder() -> Recorder:
    global _recorder
    if _recorder is None:
        _recorder = Recorder(sample_limit=get_settings().metrics_sample_limit)
    return _recorder


def reset_recorder() -> None:
    """Drop the in-process state. For tests."""
    global _recorder
    _recorder = None


async def write_process_metrics(
    session: AsyncSession, *, now: datetime | None = None
) -> list[Metric]:
    """Drain this process's counters into the current minute. Caller commits."""
    metrics = get_recorder().drain()
    await store_samples(session, metrics, now=now or datetime.now(UTC))
    return metrics
