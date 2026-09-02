"""Turning one queue row into fact rows."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.detection import evaluate_job
from app.logging import get_logger
from app.queue import ClaimedJob
from app.upserts import (
    parse_timestamp,
    propagate_workflow_id,
    run_workflow_id,
    set_installation_lifecycle,
    set_installation_repositories_active,
    set_repositories_active,
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
    run_id = job["run_id"]
    run_attempt = job.get("run_attempt") or 1

    await upsert_run(
        session,
        repo_id=repo_id,
        run_id=run_id,
        run_attempt=run_attempt,
        head_sha=head_sha,
        head_branch=job.get("head_branch"),
    )
    await upsert_job(
        session,
        repo_id=repo_id,
        job=job,
        head_sha=head_sha,
        # Signal B groups on the workflow, which only the run knows.
        workflow_id=await run_workflow_id(session, run_id=run_id),
    )
    await evaluate_job(session, repo_id=repo_id, run_id=run_id, job_name=job["name"])


async def handle_workflow_run(session: AsyncSession, payload: dict[str, Any]) -> None:
    run = payload.get("workflow_run") or {}
    if not run.get("id"):
        raise ValueError("workflow_run payload has no run")

    repo_id = await _ensure_repo(session, payload)
    workflow = payload.get("workflow") or {}
    if workflow.get("id"):
        await upsert_workflow(session, repo_id=repo_id, workflow=workflow)

    run_id = run["id"]
    run_attempt = run.get("run_attempt") or 1
    await upsert_run(
        session,
        repo_id=repo_id,
        run_id=run_id,
        run_attempt=run_attempt,
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

    # Jobs stored before this event could not know their workflow, and Signal B
    # cannot group a job whose workflow is unknown. Filling it in is what makes
    # those jobs groupable, so each one is re-evaluated.
    workflow_id = await run_workflow_id(session, run_id=run_id)
    if workflow_id is not None:
        for job_name in await propagate_workflow_id(
            session, run_id=run_id, workflow_id=workflow_id
        ):
            await evaluate_job(session, repo_id=repo_id, run_id=run_id, job_name=job_name)


async def handle_installation(session: AsyncSession, payload: dict[str, Any]) -> None:
    """install, uninstall, suspend (SPEC §7).

    The account fields arrive here and nowhere else: every other event carries
    only `installation.id`, which is why an installation row can exist as a stub
    long before this handler ever runs.
    """
    installation = payload.get("installation") or {}
    installation_id = installation.get("id")
    if not installation_id:
        raise ValueError("installation payload has no installation")

    account = installation.get("account") or {}
    await upsert_installation(
        session,
        installation_id=installation_id,
        account_id=account.get("id"),
        account_login=account.get("login"),
        account_type=account.get("type"),
    )

    action = payload.get("action")
    suspended_at = parse_timestamp(installation.get("suspended_at"))
    if action == "suspend":
        # The payload should carry the moment, but the action is the fact; falling
        # back to now() keeps a suspended install from looking live.
        suspended_at = suspended_at or datetime.now(UTC)
    elif action == "unsuspend":
        suspended_at = None

    deleted = action == "deleted"
    await set_installation_lifecycle(
        session,
        installation_id=installation_id,
        suspended_at=suspended_at,
        deleted_at=datetime.now(UTC) if deleted else None,
    )

    # An uninstall deactivates every repo of the install; nothing is deleted,
    # because the history it collected is still true.
    if deleted:
        changed = await set_installation_repositories_active(
            session, installation_id=installation_id, active=False
        )
    else:
        for repository in payload.get("repositories") or []:
            if repository.get("id"):
                await upsert_repository(
                    session,
                    installation_id=installation_id,
                    repository=repository,
                    active=True,
                )
        changed = []

    log.info(
        "worker.installation",
        installation_id=installation_id,
        action=action,
        deactivated=len(changed),
    )


async def handle_installation_repositories(
    session: AsyncSession, payload: dict[str, Any]
) -> None:
    """Repos added to or removed from an existing install (SPEC §7)."""
    installation = payload.get("installation") or {}
    installation_id = installation.get("id")
    if not installation_id:
        raise ValueError("installation_repositories payload has no installation")

    account = installation.get("account") or {}
    await upsert_installation(
        session,
        installation_id=installation_id,
        account_id=account.get("id"),
        account_login=account.get("login"),
        account_type=account.get("type"),
    )

    for repository in payload.get("repositories_added") or []:
        if repository.get("id"):
            await upsert_repository(
                session,
                installation_id=installation_id,
                repository=repository,
                active=True,
            )

    removed = [r["id"] for r in payload.get("repositories_removed") or [] if r.get("id")]
    await set_repositories_active(session, repo_ids=removed, active=False)

    log.info(
        "worker.installation_repositories",
        installation_id=installation_id,
        action=payload.get("action"),
        added=len(payload.get("repositories_added") or []),
        removed=len(removed),
    )


HANDLERS = {
    "workflow_job": handle_workflow_job,
    "workflow_run": handle_workflow_run,
    "installation": handle_installation,
    "installation_repositories": handle_installation_repositories,
}


async def handle(session: AsyncSession, claimed: ClaimedJob) -> None:
    handler = HANDLERS.get(claimed.event or "")
    if handler is None:
        log.info("worker.event_ignored", queue_id=claimed.id, github_event=claimed.event)
        return
    await handler(session, claimed.payload)
