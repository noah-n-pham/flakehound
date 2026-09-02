"""SPEC §9's counters, the minute they are sampled into, and the endpoint serving them.

The counters are only worth having if they measure the system rather than themselves,
so every test here drives real deliveries through the worker or writes real queue and
delivery rows, and then asserts the number that falls out.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import Base
from app.github import get_limiter, reset_api_state
from app.metrics import Metric, collect, latest_samples, write_snapshot
from app.models import EventQueue, MetricsSnapshot, WebhookDelivery
from app.worker import snapshot_metrics
from tests import payloads
from tests.helpers import deliver, one_session_factory
from tests.test_detection import RUN_ID, attempt, run_event

NOW = datetime(2026, 9, 2, 12, 30, 15, tzinfo=UTC)


def auth() -> dict[str, str]:
    return {"authorization": f"Bearer {get_settings().internal_api_token}"}


def value(metrics: list[Metric], name: str, **labels: str) -> float:
    matching = [m for m in metrics if m.name == name and m.labels == labels]
    assert matching, f"no series {name}{labels or ''} in {sorted({m.name for m in metrics})}"
    assert len(matching) == 1, f"{name}{labels} appears {len(matching)} times"
    return matching[0].value


def names(metrics: list[Metric]) -> set[str]:
    return {metric.name for metric in metrics}


async def stored(session, *, now: datetime = NOW) -> list[Metric]:
    """What `/internal/metrics` would serve: the newest point of every live series."""
    return [sample.metric for sample in await latest_samples(session, now=now)]


@pytest.fixture
def limiter():
    """The module-level limiter, emptied before and after so nothing leaks between tests."""
    reset_api_state()
    yield get_limiter()
    reset_api_state()


async def a_delivery(session, *, delivery_id: str, received_at: datetime, lag: float) -> None:
    """A delivery and the queue row that finished it, `lag` seconds later.

    Flushed in two steps: the queue row's foreign key names the delivery, and with no
    ORM relationship between them SQLAlchemy has nothing to order the inserts by.
    """
    session.add(
        WebhookDelivery(delivery_id=delivery_id, event="workflow_job", received_at=received_at)
    )
    await session.flush()
    session.add(
        EventQueue(
            delivery_id=delivery_id,
            job_type="webhook",
            event="workflow_job",
            payload={},
            status="done",
            completed_at=received_at + timedelta(seconds=lag),
        )
    )
    await session.flush()


# --------------------------------------------------------------------------- #
# Product counters
# --------------------------------------------------------------------------- #


async def test_the_product_counters_count_what_was_ingested(db_session):
    await deliver(
        db_session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure"),
        attempt(2, "success"),
    )

    metrics = await collect(db_session, now=NOW)

    assert value(metrics, "installations") == 1
    assert value(metrics, "installations_suspended") == 0
    assert value(metrics, "repositories_active") == 1
    assert value(metrics, "jobs") == 2
    # Two attempts of one run are two rows, because identity is (run id, attempt).
    assert value(metrics, "workflow_run_attempts") == 2
    assert value(metrics, "flake_events") == 2
    assert value(metrics, "flake_events_by_signal", signal="rerun_recovery") == 1
    assert value(metrics, "flake_events_by_signal", signal="same_commit_disagreement") == 1


async def test_an_uninstalled_installation_stops_being_counted(db_session):
    """An uninstall deactivates rather than deletes (D-037), so the counter has to read
    the flags rather than the row count."""
    await deliver(db_session, payloads.installation_event(action="created"))
    assert value(await collect(db_session, now=NOW), "installations") == 1

    await deliver(db_session, payloads.installation_event(action="deleted"))

    metrics = await collect(db_session, now=NOW)
    assert value(metrics, "installations") == 0
    assert value(metrics, "repositories_active") == 0


# --------------------------------------------------------------------------- #
# Pipeline health
# --------------------------------------------------------------------------- #


async def test_a_queue_status_with_no_rows_still_reports_a_zero(db_session):
    """A series that disappears at zero is indistinguishable from one never collected."""
    await deliver(db_session, attempt(1, "success"))

    metrics = await collect(db_session, now=NOW)

    assert value(metrics, "queue_depth", status="done") == 1
    assert value(metrics, "queue_depth", status="pending") == 0
    assert value(metrics, "queue_depth", status="processing") == 0
    assert value(metrics, "queue_depth", status="failed") == 0


async def test_a_dead_lettered_row_shows_up_as_queue_depth_failed(db_session):
    db_session.add(
        EventQueue(job_type="webhook", event="workflow_job", payload={}, status="failed")
    )
    await db_session.flush()

    assert value(await collect(db_session, now=NOW), "queue_depth", status="failed") == 1


async def test_ingest_lag_is_measured_from_receipt_to_completion(db_session):
    """SPEC §9's definition exactly: `received_at` on the delivery, `completed_at` on
    the queue row, which is the only pair that spans both processes."""
    await a_delivery(db_session, delivery_id="d1", received_at=NOW - timedelta(minutes=5), lag=2)
    await a_delivery(db_session, delivery_id="d2", received_at=NOW - timedelta(minutes=4), lag=10)

    metrics = await collect(db_session, now=NOW)

    assert value(metrics, "ingest_lag_samples") == 2
    assert value(metrics, "ingest_lag_seconds", quantile="p50") == 6.0
    assert value(metrics, "ingest_lag_seconds", quantile="p95") == pytest.approx(9.6)
    assert value(metrics, "ingest_lag_seconds", quantile="p99") == pytest.approx(9.92)


async def test_a_lag_outside_the_window_is_not_measured(db_session):
    """A percentile over all of history is a fact about the past that current slowness
    cannot move, so the window is trailing and an empty window reports no quantile."""
    await a_delivery(db_session, delivery_id="old", received_at=NOW - timedelta(days=2), lag=90)

    metrics = await collect(db_session, now=NOW, window_seconds=3600)

    assert value(metrics, "ingest_lag_samples") == 0
    assert "ingest_lag_seconds" not in names(metrics)


async def test_throughput_counts_the_rows_finished_in_the_last_interval(db_session):
    await a_delivery(db_session, delivery_id="a", received_at=NOW - timedelta(seconds=30), lag=1)
    await a_delivery(db_session, delivery_id="b", received_at=NOW - timedelta(seconds=20), lag=1)
    await a_delivery(db_session, delivery_id="stale", received_at=NOW - timedelta(hours=2), lag=1)

    metrics = await collect(db_session, now=NOW, throughput_seconds=60)

    assert value(metrics, "worker_throughput_events_per_second") == pytest.approx(2 / 60)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


async def test_every_model_table_reports_its_size(db_session):
    metrics = await collect(db_session, now=NOW)

    reported = {m.labels["table"] for m in metrics if m.name == "table_bytes"}
    assert reported == set(Base.metadata.tables)
    assert value(metrics, "table_bytes", table="jobs") > 0


async def test_rate_limit_headroom_reports_what_github_last_said(db_session, limiter):
    limiter.observe(158_221_992, {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4321"})

    metrics = await collect(db_session, now=NOW)

    headroom = value(metrics, "github_rate_limit_headroom", installation_id="158221992")
    # Not exactly 4321: the bucket paces requests, so it has been refilling since the
    # header was read. What matters is that it tracks GitHub's number rather than ours.
    assert headroom == pytest.approx(4321, abs=1.0)


async def test_an_installation_nobody_has_called_has_no_headroom_series(db_session, limiter):
    """Reporting the default as though it were a measurement would be worse than silence."""
    metrics = await collect(db_session, now=NOW)

    assert "github_rate_limit_headroom" not in names(metrics)


# --------------------------------------------------------------------------- #
# The sample
# --------------------------------------------------------------------------- #


async def test_no_two_series_share_a_name_and_labels(db_session):
    """The snapshot upserts a whole minute in one statement, and Postgres refuses to let
    one statement touch a row twice — so a collision here is a runtime error, not a
    duplicate row."""
    await deliver(db_session, attempt(1, "success"))

    metrics = await collect(db_session, now=NOW)

    keys = [(m.name, tuple(sorted(m.labels.items()))) for m in metrics]
    assert len(keys) == len(set(keys))


async def test_a_sample_lands_on_the_minute_it_was_taken(db_session):
    await write_snapshot(db_session, now=NOW)

    captured = (await db_session.execute(select(MetricsSnapshot.captured_at).limit(1))).scalar_one()
    assert captured == NOW.replace(second=0, microsecond=0)


async def test_a_second_pass_in_one_minute_corrects_the_sample_rather_than_doubling_it(db_session):
    await deliver(db_session, attempt(1, "success"))
    first = await write_snapshot(db_session, now=NOW)
    rows_after_first = await db_session.scalar(select(func.count()).select_from(MetricsSnapshot))

    await deliver(db_session, attempt(2, "success"))
    await write_snapshot(db_session, now=NOW + timedelta(seconds=20))

    rows_after_second = await db_session.scalar(select(func.count()).select_from(MetricsSnapshot))
    assert rows_after_first == len(first)
    assert rows_after_second == rows_after_first

    assert value(await stored(db_session), "jobs") == 2


async def test_samples_older_than_the_retention_window_are_pruned(db_session):
    ancient = NOW - timedelta(days=get_settings().metrics_retention_days + 1)
    db_session.add(MetricsSnapshot(captured_at=ancient, name="jobs", value=1, labels={}))
    await db_session.flush()

    await write_snapshot(db_session, now=NOW)

    remaining = (
        await db_session.execute(select(func.min(MetricsSnapshot.captured_at)))
    ).scalar_one()
    assert remaining == NOW.replace(second=0, microsecond=0)


async def test_a_series_reports_its_newest_point(db_session):
    await deliver(db_session, attempt(1, "success"))
    await write_snapshot(db_session, now=NOW - timedelta(minutes=1))
    await deliver(db_session, attempt(2, "success"))
    await write_snapshot(db_session, now=NOW)

    samples = await latest_samples(db_session, now=NOW)
    jobs = next(sample for sample in samples if sample.metric.name == "jobs")

    assert jobs.metric.value == 2
    assert jobs.captured_at == NOW.replace(second=0, microsecond=0)


async def test_a_series_whose_writer_stopped_drops_out_rather_than_looking_current(db_session):
    """A number with no time attached is worse than an absence."""
    await write_snapshot(db_session, now=NOW - timedelta(hours=1))

    assert await stored(db_session) == []
    assert await stored(db_session, now=NOW - timedelta(hours=1)) != []


async def test_the_worker_writes_a_sample(db_session):
    """The wiring: the worker's own call, not the function it happens to use."""
    await deliver(db_session, attempt(1, "success"))

    written = await snapshot_metrics(one_session_factory(db_session))

    points = await stored(db_session, now=datetime.now(UTC))
    assert value(points, "jobs") == 1
    assert names(points) == names(written)


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #


async def test_the_metrics_endpoint_needs_the_token(client):
    """SPEC §8: "internal" is a name, not a boundary. The tunnel routes the hostname."""
    response = await client.get("/internal/metrics")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_the_metrics_endpoint_serves_the_latest_sample(client, db_session):
    await deliver(db_session, attempt(1, "success"))
    await write_snapshot(db_session)

    response = await client.get("/internal/metrics", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert body["captured_at"] is not None
    assert 0 <= body["age_seconds"] < 120
    jobs = [point for point in body["metrics"] if point["name"] == "jobs"]
    assert len(jobs) == 1
    assert (jobs[0]["value"], jobs[0]["labels"]) == (1.0, {})
    assert jobs[0]["captured_at"] == body["captured_at"]


async def test_the_endpoint_is_honest_before_the_first_sample(client, db_session):
    response = await client.get("/internal/metrics", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"captured_at": None, "age_seconds": None, "metrics": []}
