"""Idempotency layer 2: every fact write is an upsert on GitHub's own ids (SPEC §6).

GitHub's ids are immutable and authoritative, so replaying any payload converges
to identical state. Enrichment fields use COALESCE(EXCLUDED, existing) wherever a
later payload may know less than an earlier one — a `workflow_job` event carries
no workflow id, and must never blank one a `workflow_run` event already supplied.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Installation, Job, Repository, Workflow, WorkflowRun


def parse_timestamp(value: Any) -> datetime | None:
    """GitHub sends RFC 3339 with a `Z`, which fromisoformat handles on 3.11+."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def upsert_installation(
    session: AsyncSession,
    *,
    installation_id: int,
    account_id: int | None = None,
    account_login: str | None = None,
    account_type: str | None = None,
) -> None:
    stmt = insert(Installation).values(
        id=installation_id,
        account_id=account_id,
        account_login=account_login,
        account_type=account_type,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Installation.id],
            set_={
                "account_id": func.coalesce(stmt.excluded.account_id, Installation.account_id),
                "account_login": func.coalesce(
                    stmt.excluded.account_login, Installation.account_login
                ),
                "account_type": func.coalesce(
                    stmt.excluded.account_type, Installation.account_type
                ),
                "updated_at": func.now(),
            },
        )
    )


async def upsert_repository(
    session: AsyncSession, *, installation_id: int, repository: dict[str, Any]
) -> int:
    owner = repository.get("owner") or {}
    full_name = repository.get("full_name") or ""
    stmt = insert(Repository).values(
        id=repository["id"],
        installation_id=installation_id,
        owner=owner.get("login") or full_name.split("/")[0],
        name=repository.get("name") or full_name.rpartition("/")[2],
        full_name=full_name,
        private=bool(repository.get("private", True)),
        default_branch=repository.get("default_branch"),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Repository.id],
            set_={
                "installation_id": stmt.excluded.installation_id,
                "owner": stmt.excluded.owner,
                "name": stmt.excluded.name,
                "full_name": stmt.excluded.full_name,
                "private": stmt.excluded.private,
                "default_branch": func.coalesce(
                    stmt.excluded.default_branch, Repository.default_branch
                ),
                "updated_at": func.now(),
            },
        )
    )
    return int(repository["id"])


async def upsert_workflow(
    session: AsyncSession, *, repo_id: int, workflow: dict[str, Any]
) -> None:
    stmt = insert(Workflow).values(
        id=workflow["id"],
        repo_id=repo_id,
        name=workflow.get("name"),
        path=workflow.get("path"),
        state=workflow.get("state"),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Workflow.id],
            set_={
                "name": func.coalesce(stmt.excluded.name, Workflow.name),
                "path": func.coalesce(stmt.excluded.path, Workflow.path),
                "state": func.coalesce(stmt.excluded.state, Workflow.state),
                "updated_at": func.now(),
            },
        )
    )


async def upsert_run(
    session: AsyncSession,
    *,
    repo_id: int,
    run_id: int,
    run_attempt: int,
    head_sha: str,
    workflow_id: int | None = None,
    head_branch: str | None = None,
    event: str | None = None,
    status: str | None = None,
    conclusion: str | None = None,
    run_started_at: datetime | None = None,
    github_created_at: datetime | None = None,
    github_updated_at: datetime | None = None,
) -> None:
    """Upsert one run attempt. Also used to stub a run from a job payload (D-005).

    Every field a job payload cannot supply is merged with COALESCE, so the stub
    never erases what a run event already recorded.
    """
    stmt = insert(WorkflowRun).values(
        run_id=run_id,
        run_attempt=run_attempt,
        repo_id=repo_id,
        workflow_id=workflow_id,
        head_sha=head_sha,
        head_branch=head_branch,
        event=event,
        status=status,
        conclusion=conclusion,
        run_started_at=run_started_at,
        github_created_at=github_created_at,
        github_updated_at=github_updated_at,
    )
    merged = {
        column: func.coalesce(stmt.excluded[column], getattr(WorkflowRun, column))
        for column in (
            "workflow_id",
            "head_branch",
            "event",
            "status",
            "conclusion",
            "run_started_at",
            "github_created_at",
            "github_updated_at",
        )
    }
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[WorkflowRun.run_id, WorkflowRun.run_attempt],
            set_={"head_sha": stmt.excluded.head_sha, "updated_at": func.now(), **merged},
        )
    )


async def upsert_job(
    session: AsyncSession,
    *,
    repo_id: int,
    job: dict[str, Any],
    head_sha: str,
    workflow_id: int | None = None,
) -> None:
    steps = job.get("steps") or []
    labels = job.get("labels")
    stmt = insert(Job).values(
        id=job["id"],
        run_id=job["run_id"],
        run_attempt=job.get("run_attempt") or 1,
        repo_id=repo_id,
        workflow_id=workflow_id,
        head_sha=head_sha,
        # Stored whole, matrix values included. Never normalized or split.
        name=job["name"],
        status=job.get("status"),
        conclusion=job.get("conclusion"),
        started_at=parse_timestamp(job.get("started_at")),
        completed_at=parse_timestamp(job.get("completed_at")),
        runner_name=job.get("runner_name"),
        runner_labels=list(labels) if isinstance(labels, list) else None,
        step_count=len(steps) if steps else None,
        completed_step_count=sum(1 for s in steps if s.get("status") == "completed") or None,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Job.id],
            set_={
                "status": stmt.excluded.status,
                "conclusion": stmt.excluded.conclusion,
                "started_at": func.coalesce(stmt.excluded.started_at, Job.started_at),
                "completed_at": func.coalesce(stmt.excluded.completed_at, Job.completed_at),
                "runner_name": func.coalesce(stmt.excluded.runner_name, Job.runner_name),
                "runner_labels": func.coalesce(stmt.excluded.runner_labels, Job.runner_labels),
                "step_count": func.coalesce(stmt.excluded.step_count, Job.step_count),
                "completed_step_count": func.coalesce(
                    stmt.excluded.completed_step_count, Job.completed_step_count
                ),
                "workflow_id": func.coalesce(stmt.excluded.workflow_id, Job.workflow_id),
                "updated_at": func.now(),
            },
        )
    )
