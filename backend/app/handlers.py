"""Turning one queue row into fact rows."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.queue import ClaimedJob
from app.upserts import (
    parse_timestamp,
    upsert_installation,
    upsert_job,
    upsert_repository,
    upsert_run,
    upsert_workflow,
)

log = get_logger(__name__)


def _installation_id(payload: dict[str, Any]) -> int | None:
    installation = payload.get("installation")
    return installation.get("id") if isinstance(installation, dict) else None


async def _ensure_repo(session: AsyncSession, payload: dict[str, Any]) -> int:
    repository = payload.get("repository") or {}
    installation_id = _installation_id(payload)
    if not repository.get("id") or installation_id is None:
        raise ValueError("payload has no repository or installation")

    owner = repository.get("owner") or {}
    await upsert_installation(
        session,
        installation_id=installation_id,
        account_id=owner.get("id"),
        account_login=owner.get("login"),
        account_type=owner.get("type"),
    )
    return await upsert_repository(
        session, installation_id=installation_id, repository=repository
    )


async def handle_workflow_job(session: AsyncSession, payload: dict[str, Any]) -> None:
    """A job event carries no workflow id, so its run is stubbed first (D-005)."""
    job = payload.get("workflow_job") or {}
    if not job.get("id"):
        raise ValueError("workflow_job payload has no job")

    repo_id = await _ensure_repo(session, payload)
    head_sha = job["head_sha"]

    await upsert_run(
        session,
        repo_id=repo_id,
        run_id=job["run_id"],
        run_attempt=job.get("run_attempt") or 1,
        head_sha=head_sha,
        head_branch=job.get("head_branch"),
    )
    await upsert_job(session, repo_id=repo_id, job=job, head_sha=head_sha)


async def handle_workflow_run(session: AsyncSession, payload: dict[str, Any]) -> None:
    run = payload.get("workflow_run") or {}
    if not run.get("id"):
        raise ValueError("workflow_run payload has no run")

    repo_id = await _ensure_repo(session, payload)
    workflow = payload.get("workflow") or {}
    if workflow.get("id"):
        await upsert_workflow(session, repo_id=repo_id, workflow=workflow)

    await upsert_run(
        session,
        repo_id=repo_id,
        run_id=run["id"],
        run_attempt=run.get("run_attempt") or 1,
        head_sha=run["head_sha"],
        workflow_id=run.get("workflow_id") or workflow.get("id"),
        head_branch=run.get("head_branch"),
        event=run.get("event"),
        status=run.get("status"),
        conclusion=run.get("conclusion"),
        run_started_at=parse_timestamp(run.get("run_started_at")),
        github_created_at=parse_timestamp(run.get("created_at")),
        github_updated_at=parse_timestamp(run.get("updated_at")),
    )


HANDLERS = {
    "workflow_job": handle_workflow_job,
    "workflow_run": handle_workflow_run,
}


async def handle(session: AsyncSession, claimed: ClaimedJob) -> None:
    handler = HANDLERS.get(claimed.event or "")
    if handler is None:
        log.info("worker.event_ignored", queue_id=claimed.id, github_event=claimed.event)
        return
    await handler(session, claimed.payload)
