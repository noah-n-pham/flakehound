"""The half of the metrics story that only the API process can see.

Latency per endpoint and the duplicate-delivery rate are measured in memory, because
neither can be computed from the database: a request's duration is gone the moment it
is answered, and a duplicate delivery is one whose insert *failed*, so nothing was
written to count.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.apimetrics import (
    DELIVERIES_DUPLICATE,
    DELIVERIES_RECEIVED,
    QUANTILES,
    UNMATCHED,
    Recorder,
    Reservoir,
    get_recorder,
    quantile,
    reset_recorder,
    write_process_metrics,
)
from app.config import get_settings
from app.metrics import latest_samples, write_snapshot
from tests import payloads
from tests.test_metrics import auth, names, value

NOW = datetime(2026, 9, 2, 12, 30, 15, tzinfo=UTC)


@pytest.fixture(autouse=True)
def recorder():
    """Module-level state, so it is emptied around every test in this file."""
    reset_recorder()
    yield get_recorder()
    reset_recorder()


def signed(payload: dict, delivery_id: str) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    digest = hmac.new(
        get_settings().github_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return body, {
        "x-github-delivery": delivery_id,
        "x-github-event": "workflow_job",
        "x-hub-signature-256": f"sha256={digest}",
        "content-type": "application/json",
    }


# --------------------------------------------------------------------------- #
# The percentile and the reservoir
# --------------------------------------------------------------------------- #


async def test_the_quantile_matches_postgres_percentile_cont(db_session):
    """Ingest lag and API latency are read side by side, so p95 must mean one thing.

    Compared against Postgres itself rather than against numbers I believe Postgres
    would produce — the claim is that the two implementations agree, and only one of
    them is in this repository.
    """
    samples = [0.004, 0.011, 0.02, 0.35, 1.2, 0.07, 0.09, 0.152]

    for _, q in QUANTILES:
        postgres = await db_session.scalar(
            text(
                "SELECT percentile_cont(:q) WITHIN GROUP (ORDER BY v) "
                "FROM unnest(CAST(:samples AS double precision[])) AS v"
            ),
            {"q": q, "samples": samples},
        )
        assert quantile(samples, q) == pytest.approx(postgres)


def test_the_quantile_does_not_depend_on_arrival_order():
    assert quantile([3.0, 1.0, 2.0], 0.5) == quantile([1.0, 2.0, 3.0], 0.5) == 2.0


def test_a_single_sample_is_every_quantile():
    assert quantile([0.7], 0.5) == quantile([0.7], 0.99) == 0.7


def test_the_reservoir_is_bounded_but_counts_everything():
    """Memory is what the limit protects; the count stays true regardless."""
    reservoir = Reservoir(limit=100)

    for i in range(5_000):
        reservoir.add(float(i))

    assert reservoir.seen == 5_000
    assert len(reservoir.samples) == 100


def test_the_reservoir_samples_the_whole_minute_not_its_beginning():
    """A "keep the first N" cap would quietly report the quietest part of the minute.

    Ten thousand observations, the last quarter of them distinguishable: an unbiased
    reservoir should hold roughly a quarter of them, and a first-N cap none at all.
    """
    reservoir = Reservoir(limit=200)
    for i in range(10_000):
        reservoir.add(1.0 if i >= 7_500 else 0.0)

    late = sum(1 for sample in reservoir.samples if sample == 1.0)

    assert 0.15 < late / len(reservoir.samples) < 0.35


# --------------------------------------------------------------------------- #
# What a drain says
# --------------------------------------------------------------------------- #


def test_an_idle_minute_still_reports_its_totals():
    metrics = Recorder().drain()

    assert value(metrics, "api_requests") == 0
    assert value(metrics, DELIVERIES_RECEIVED) == 0
    assert value(metrics, "duplicate_delivery_rate") == 0.0
    # No endpoint was called, and there is no list of endpoints that were not.
    assert "api_request_seconds" not in names(metrics)


def test_latency_is_reported_per_endpoint():
    recorder = Recorder()
    for seconds in (0.01, 0.02, 0.03):
        recorder.observe_request("/api/repos/{repo_id}/flaky", seconds)
    recorder.observe_request("/healthz", 0.001)

    metrics = recorder.drain()

    assert value(metrics, "api_requests") == 4
    assert (
        value(metrics, "api_requests_by_endpoint", endpoint="/api/repos/{repo_id}/flaky") == 3
    )
    assert (
        value(
            metrics,
            "api_request_seconds",
            endpoint="/api/repos/{repo_id}/flaky",
            quantile="p50",
        )
        == 0.02
    )
    assert value(metrics, "api_request_seconds", endpoint="/healthz", quantile="p99") == 0.001


def test_a_drain_resets_the_minute():
    recorder = Recorder()
    recorder.observe_request("/healthz", 0.5)
    recorder.count(DELIVERIES_RECEIVED)

    recorder.drain()
    second = recorder.drain()

    assert value(second, "api_requests") == 0
    assert value(second, DELIVERIES_RECEIVED) == 0
    assert "api_request_seconds" not in names(second)


def test_no_two_series_share_a_name_and_labels():
    """One statement writes the whole minute, and Postgres refuses to touch a row twice."""
    recorder = Recorder()
    recorder.observe_request("/healthz", 0.1)
    recorder.observe_request("/api/repos", 0.2)
    recorder.count(DELIVERIES_RECEIVED)

    keys = [(m.name, tuple(sorted(m.labels.items()))) for m in recorder.drain()]

    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------- #
# Through the real app
# --------------------------------------------------------------------------- #


async def test_the_route_template_is_the_label_not_the_path(client, db_session, recorder):
    """One series per repository id would be a new series every time a repo is added."""
    await client.get(f"/api/repos/{payloads.REPO_ID}/flaky", headers=auth())

    metrics = recorder.drain()

    assert value(metrics, "api_requests_by_endpoint", endpoint="/api/repos/{repo_id}/flaky") == 1


async def test_an_unmatched_path_collapses_into_one_label(client, recorder):
    """Otherwise anything a scanner throws at the tunnel becomes a permanent series."""
    await client.get("/wp-login.php")
    await client.get("/.env")

    metrics = recorder.drain()

    assert value(metrics, "api_requests_by_endpoint", endpoint=UNMATCHED) == 2


async def test_a_request_that_failed_still_contributes_its_time(client, recorder):
    """A 401 spent real time and is exactly the request you want the latency of."""
    response = await client.get("/internal/metrics")

    assert response.status_code == 401
    assert value(recorder.drain(), "api_requests_by_endpoint", endpoint="/internal/metrics") == 1


async def test_the_duplicate_delivery_rate_counts_what_the_database_cannot(client, db_session):
    """The second delivery of one id is dropped by the primary key, leaving no row."""
    body, headers = signed(payloads.workflow_job(), "delivery-abc")

    first = await client.post("/webhooks/github", content=body, headers=headers)
    second = await client.post("/webhooks/github", content=body, headers=headers)

    assert (first.json()["status"], second.json()["status"]) == ("queued", "duplicate")

    metrics = get_recorder().drain()
    assert value(metrics, DELIVERIES_RECEIVED) == 2
    assert value(metrics, DELIVERIES_DUPLICATE) == 1
    assert value(metrics, "duplicate_delivery_rate") == 0.5


# --------------------------------------------------------------------------- #
# Two writers, one table
# --------------------------------------------------------------------------- #


async def test_both_processes_series_survive_writing_in_different_minutes(client, db_session):
    """The reason the endpoint reports per series rather than per minute.

    The worker's timer and the API's are independent, so one of them writes into the
    minute the other has already left. Taking "the latest minute" would drop whichever
    wrote first, intermittently and for no reason a reader could guess.
    """
    get_recorder().observe_request("/healthz", 0.004)

    await write_snapshot(db_session, now=NOW - timedelta(minutes=1))
    await write_process_metrics(db_session, now=NOW)

    samples = {
        sample.metric.name: sample for sample in await latest_samples(db_session, now=NOW)
    }

    assert samples["jobs"].captured_at == (NOW - timedelta(minutes=1)).replace(
        second=0, microsecond=0
    )
    assert samples["api_requests"].captured_at == NOW.replace(second=0, microsecond=0)
    assert samples["api_requests"].metric.value == 1


async def test_the_endpoint_serves_both_halves(client, db_session):
    get_recorder().observe_request("/api/repos", 0.012)
    await write_snapshot(db_session)
    await write_process_metrics(db_session)

    body = (await client.get("/internal/metrics", headers=auth())).json()
    served = {point["name"] for point in body["metrics"]}

    assert {"jobs", "queue_depth", "ingest_lag_samples"} <= served
    assert {"api_requests", "api_request_seconds", "duplicate_delivery_rate"} <= served
