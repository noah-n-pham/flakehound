"""Read endpoints and the internal bearer token (SPEC §8)."""

from app.config import get_settings
from app.models import Installation, Job, Repository, WorkflowRun
from app.upserts import parse_timestamp
from tests import payloads


def auth() -> dict[str, str]:
    return {"authorization": f"Bearer {get_settings().internal_api_token}"}


async def seed_jobs(session, count: int = 2) -> None:
    session.add(Installation(id=payloads.INSTALLATION_ID, account_login="khoi"))
    session.add(
        Repository(
            id=payloads.REPO_ID,
            installation_id=payloads.INSTALLATION_ID,
            owner="khoi",
            name="flakehound",
            full_name="khoi/flakehound",
            private=False,
        )
    )
    session.add(
        WorkflowRun(
            run_id=payloads.RUN_ID,
            run_attempt=1,
            repo_id=payloads.REPO_ID,
            head_sha=payloads.SHA,
        )
    )
    await session.flush()
    for i in range(count):
        session.add(
            Job(
                id=payloads.JOB_ID + i,
                run_id=payloads.RUN_ID,
                run_attempt=1,
                repo_id=payloads.REPO_ID,
                head_sha=payloads.SHA,
                name=f"test (ubuntu-latest, 3.1{i})",
                status="completed",
                conclusion="success",
                started_at=parse_timestamp(f"2026-08-31T14:0{i}:00Z"),
                completed_at=parse_timestamp(f"2026-08-31T14:0{i + 1}:30Z"),
            )
        )
    await session.flush()


async def test_reads_without_a_token_are_rejected(client, db_session):
    await seed_jobs(db_session)

    response = await client.get(f"/api/repos/{payloads.REPO_ID}/jobs")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_reads_with_the_wrong_token_are_rejected(client):
    response = await client.get("/api/repos", headers={"authorization": "Bearer wrong"})
    assert response.status_code == 401


async def test_a_token_without_the_bearer_scheme_is_rejected(client):
    token = get_settings().internal_api_token
    response = await client.get("/api/repos", headers={"authorization": token})
    assert response.status_code == 401


async def test_the_repo_list_summarizes_real_rows(client, db_session):
    await seed_jobs(db_session, count=3)

    response = await client.get("/api/repos", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["full_name"] == "khoi/flakehound"
    assert body[0]["private"] is False
    assert body[0]["job_count"] == 3
    assert body[0]["last_job_at"] is not None


async def test_the_jobs_endpoint_returns_real_job_rows(client, db_session):
    await seed_jobs(db_session, count=2)

    response = await client.get(f"/api/repos/{payloads.REPO_ID}/jobs", headers=auth())

    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 2
    # Newest first.
    assert jobs[0]["name"] == "test (ubuntu-latest, 3.11)"
    assert jobs[0]["conclusion"] == "success"
    assert jobs[0]["head_sha"] == payloads.SHA
    assert jobs[0]["duration_seconds"] == 90.0
    assert jobs[0]["run_attempt"] == 1


async def test_the_jobs_endpoint_honours_its_limit(client, db_session):
    await seed_jobs(db_session, count=3)

    response = await client.get(
        f"/api/repos/{payloads.REPO_ID}/jobs", params={"limit": 1}, headers=auth()
    )

    assert response.status_code == 200
    assert len(response.json()) == 1


async def test_an_unknown_repo_is_a_404_not_an_empty_list(client, db_session):
    await seed_jobs(db_session)

    response = await client.get("/api/repos/1234567/jobs", headers=auth())

    assert response.status_code == 404


async def test_healthz_stays_unauthenticated(client):
    assert (await client.get("/healthz")).status_code == 200
