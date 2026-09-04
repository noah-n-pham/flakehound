"""The control channel that replaces `aws ecs run-task`.

There is no shell on the instance and nothing may dial in, so this token-gated surface
is the only way the build drives the observational crawl. Two properties matter more
than the endpoints themselves: it must be *bounded*, because the roadmap forbids
dumping the pool onto the queue in one go, and it must be *narrow*: a named operation
with typed arguments, never a query or a command, because it is reachable from the
internet with a bearer token.
"""

import pytest
from sqlalchemy import select

from app.backfill import OBSERVED_PRIORITY, RUNS_JOB_TYPE
from app.config import get_settings
from app.discover import BANDS, get_search_limiter, reset_search_limiter
from app.github import reset_api_state
from app.models import EventQueue, FlakeEvent, Job, Repository, WorkflowRun
from app.ops import DISCOVER_JOB_TYPE, OPS_PRIORITY, corpus_counts
from app.queue import claim_batch
from app.upserts import upsert_repository
from tests.conftest import INSTALLATION_ID
from tests.test_api import auth
from tests.test_discover import stub_candidate, stub_search
from tests.test_observed_backfill import installed_repo


@pytest.fixture(autouse=True)
def fresh_limiters():
    reset_search_limiter()
    reset_api_state()
    yield
    reset_search_limiter()
    reset_api_state()


@pytest.fixture
def observation_identity(monkeypatch):
    monkeypatch.setenv("OBSERVATION_INSTALLATION_ID", str(INSTALLATION_ID))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def observed(session, repo_id: int, full_name: str, status: str = "pending") -> Repository:
    await upsert_repository(
        session,
        installation_id=None,
        source="observed",
        repository={"id": repo_id, "full_name": full_name, "private": False},
    )
    await session.flush()
    repo = await session.get(Repository, repo_id)
    repo.backfill_status = status
    await session.flush()
    return repo


# --------------------------------------------------------------------------- #
# The readout
# --------------------------------------------------------------------------- #


async def test_corpus_counts_observed_repos_by_crawl_state(client, db_session):
    await observed(db_session, 1, "a/one", status="done")
    await observed(db_session, 2, "a/two", status="done")
    await observed(db_session, 3, "a/three", status="running")
    await observed(db_session, 4, "a/four", status="pending")

    body = (await client.get("/internal/corpus", headers=auth())).json()

    assert body["observed"] == {"done": 2, "running": 1, "pending": 1, "total": 4}


async def test_corpus_facts_count_only_observed_repos(client, db_session):
    """The corpus is the observational board's, so an installed repo's rows are not it.

    Without the filter the number Section I reports would silently include the
    dogfooded repository's own CI history, which is not part of the public corpus.
    """
    repo = await observed(db_session, 1, "a/one", status="done")
    installed = await installed_repo(db_session)

    for owner_id in (repo.id, installed.id):
        db_session.add(
            WorkflowRun(repo_id=owner_id, run_id=100 + owner_id, run_attempt=1, head_sha="abc")
        )
        await db_session.flush()
        db_session.add(
            Job(
                id=900 + owner_id,
                repo_id=owner_id,
                run_id=100 + owner_id,
                run_attempt=1,
                name="test",
                head_sha="abc",
            )
        )
        db_session.add(
            FlakeEvent(
                repo_id=owner_id,
                signal="rerun_recovery",
                run_id=100 + owner_id,
                job_name="test",
                evidence={},
            )
        )
    await db_session.flush()

    body = (await client.get("/internal/corpus", headers=auth())).json()

    assert body["observed_facts"] == {"workflow_runs": 1, "jobs": 1, "flake_events": 1}


async def test_corpus_reports_the_queue_draining_into_it(client, db_session):
    db_session.add(EventQueue(job_type="webhook", event="workflow_job", payload={}))
    db_session.add(EventQueue(job_type=RUNS_JOB_TYPE, payload={}, status="done"))
    await db_session.flush()

    body = (await client.get("/internal/corpus", headers=auth())).json()

    assert body["queue"] == {"pending": 1, "done": 1}


async def test_corpus_counts_match_the_function_the_page_will_use(client, db_session):
    """The endpoint must not become a second, drifting definition of the corpus."""
    await observed(db_session, 1, "a/one", status="done")

    counts = await corpus_counts(db_session)
    body = (await client.get("/internal/corpus", headers=auth())).json()

    assert counts.observed_done == 1
    assert body["observed"] == counts.observed


# --------------------------------------------------------------------------- #
# Starting a crawl tranche
# --------------------------------------------------------------------------- #


async def test_observed_crawl_queues_a_bounded_tranche(client, db_session):
    for repo_id in range(1, 6):
        await observed(db_session, repo_id, f"a/repo{repo_id}")

    body = (
        await client.post("/internal/ops/observed-crawl", json={"limit": 3}, headers=auth())
    ).json()

    assert body["count"] == 3
    assert len(body["started"]) == 3
    queued = (
        await db_session.execute(
            select(EventQueue).where(EventQueue.job_type == RUNS_JOB_TYPE)
        )
    ).scalars().all()
    assert len(queued) == 3
    # Below live events and below a real user's history, still.
    assert {row.priority for row in queued} == {OBSERVED_PRIORITY}


async def test_a_second_call_does_not_restart_a_crawl_already_under_way(client, db_session):
    for repo_id in range(1, 4):
        await observed(db_session, repo_id, f"a/repo{repo_id}")

    first = (
        await client.post("/internal/ops/observed-crawl", json={"limit": 2}, headers=auth())
    ).json()
    second = (
        await client.post("/internal/ops/observed-crawl", json={"limit": 2}, headers=auth())
    ).json()

    assert first["count"] == 2
    # Only the one still pending; the other two moved to running on the first call.
    assert second["count"] == 1
    assert set(first["started"]).isdisjoint(second["started"])


async def test_the_tranche_ceiling_is_a_constraint_not_a_convention(client):
    """The roadmap forbids dumping the whole pool on the queue. So the schema does."""
    response = await client.post(
        "/internal/ops/observed-crawl", json={"limit": 5000}, headers=auth()
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


async def test_discovery_is_queued_one_row_per_band(client, db_session):
    """One row per band, not one per pass: a full pass would sit on the reaper's edge."""
    body = (await client.post("/internal/ops/discover", json={}, headers=auth())).json()

    assert len(body["queued"]) == len(BANDS)
    rows = (
        await db_session.execute(
            select(EventQueue).where(EventQueue.job_type == DISCOVER_JOB_TYPE)
        )
    ).scalars().all()
    assert [row.payload["band"] for row in rows] == [band.name for band in BANDS]
    assert {row.priority for row in rows} == {OPS_PRIORITY}


async def test_an_operator_command_never_overtakes_a_live_delivery(client, db_session):
    """Same priority tier as live, so the tiebreak is age and the webhook is older.

    Claiming one row at a time, because `claim_batch` only promises which rows a batch
    selects. The order they come back in is the UPDATE's, not the ORDER BY's.
    """
    db_session.add(EventQueue(job_type="webhook", event="workflow_job", payload={}))
    await db_session.flush()

    await client.post("/internal/ops/discover", json={"bands": ["small"]}, headers=auth())

    assert [job.job_type for job in await claim_batch(db_session, 1)] == ["webhook"]
    assert [job.job_type for job in await claim_batch(db_session, 1)] == [DISCOVER_JOB_TYPE]


async def test_a_command_still_beats_the_crawl_backlog(client, db_session):
    """The reason it is not at the crawl's priority: it would never be claimed."""
    for _ in range(3):
        db_session.add(EventQueue(job_type=RUNS_JOB_TYPE, payload={}, priority=OBSERVED_PRIORITY))
    await db_session.flush()

    await client.post("/internal/ops/discover", json={"bands": ["small"]}, headers=auth())

    assert [job.job_type for job in await claim_batch(db_session, 1)] == [DISCOVER_JOB_TYPE]


async def test_an_unknown_band_is_rejected_on_the_request_that_made_it(client, db_session):
    response = await client.post(
        "/internal/ops/discover", json={"bands": ["enormous"]}, headers=auth()
    )

    assert response.status_code == 400
    assert "enormous" in response.json()["detail"]
    assert (await db_session.execute(select(EventQueue))).scalars().all() == []


async def test_per_band_is_bounded_too(client):
    response = await client.post(
        "/internal/ops/discover", json={"per_band": 10_000}, headers=auth()
    )

    assert response.status_code == 422


async def test_a_queued_discovery_row_admits_a_repo_through_the_worker(
    client, db_session, respx_mock, token_route, app_credentials, observation_identity
):
    """End to end: the endpoint queues, the worker's dispatcher runs the band."""
    from app.handlers import handle

    get_search_limiter()
    stub_search(respx_mock, "owner/candidate")
    stub_candidate(respx_mock, "owner/candidate", 4242)

    await client.post(
        "/internal/ops/discover", json={"bands": ["lower"], "per_band": 1}, headers=auth()
    )
    claimed = await claim_batch(db_session, 10)
    await handle(db_session, claimed[0])

    admitted = await db_session.get(Repository, 4242)
    assert admitted is not None
    assert admitted.source == "observed"
    assert admitted.installation_id is None


# --------------------------------------------------------------------------- #
# Nothing here is public
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/internal/corpus"),
        ("post", "/internal/ops/observed-crawl"),
        ("post", "/internal/ops/discover"),
    ],
)
async def test_the_control_channel_requires_the_internal_token(client, method, path):
    response = await client.request(method, path, json={})

    assert response.status_code == 401


async def test_a_wrong_token_is_not_enough(client):
    response = await client.get(
        "/internal/corpus", headers={"authorization": "Bearer not-the-token"}
    )

    assert response.status_code == 401


def test_no_operation_takes_free_text():
    """The guard against this becoming a remote shell, asserted rather than assumed.

    If a later slice adds an operation whose argument is a query, a command, or a
    path, this fails and the reviewer has to say so out loud.
    """
    from app.routes.internal import CrawlRequest, DiscoverRequest

    assert set(CrawlRequest.model_fields) == {"limit", "days"}
    assert set(DiscoverRequest.model_fields) == {"bands", "per_band"}
    # `bands` is the only string input and it is resolved against a closed set.
    assert all(
        field.annotation in (int, int | None, list[str])
        for field in (*CrawlRequest.model_fields.values(), *DiscoverRequest.model_fields.values())
    )
