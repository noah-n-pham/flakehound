"""Idempotency layer 2: every fact write is an upsert on GitHub's own ids (SPEC §6).

GitHub's ids are immutable and authoritative, so replaying any payload converges
to identical state. Enrichment fields use COALESCE(EXCLUDED, existing) wherever a
later payload may know less than an earlier one — a `workflow_job` event carries
no workflow id, and must never blank one a `workflow_run` event already supplied.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Installation, Job, Repository, Workflow, WorkflowRun


def _status_rank(column: Any) -> Any:
    """How far along an execution a payload claims to be.

    GitHub sends three deliveries per job and per run — queued, in progress, completed —
    and nothing guarantees they are *processed* in that order. `claim_batch` orders which
    rows it claims but `RETURNING` gives no ordering guarantee within a batch, and the
    reaper can re-run one message beside another. So a payload's own status is the only
    reliable statement of how much it knows.
    """
    return case((column == "completed", 3), (column == "in_progress", 2), else_=1)


def _never_regress(
    excluded: Any,
    table: Any,
    progress_columns: tuple[str, ...],
    *,
    always_merge: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Merge a payload only when it is at least as advanced as the row already stored.

    A payload that is *behind* what we have contributes nothing: an `in_progress` body
    carries a null conclusion and a partial step count, and applying it after the
    `completed` body would erase a terminal fact. That is not a hypothetical — it
    happened in production and turned a successful job run into one with no conclusion,
    which silently removed it from Signal A's opportunities (turn 19).

    `always_merge` columns are exempt because they carry identity rather than progress:
    a workflow id is equally true in every delivery, and whichever one supplies it first
    is right.
    """
    advanced = _status_rank(excluded.status) >= _status_rank(table.status)
    merged: dict[str, Any] = {
        column: case(
            (advanced, func.coalesce(excluded[column], getattr(table, column))),
            else_=getattr(table, column),
        )
        for column in progress_columns
    }
    # Status itself advances rather than merges: it is never null, so COALESCE would
    # freeze it at whichever delivery landed first.
    merged["status"] = case((advanced, excluded.status), else_=table.status)
    merged.update(
        {
            column: func.coalesce(excluded[column], getattr(table, column))
            for column in always_merge
        }
    )
    return merged


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
    merged = _never_regress(
        stmt.excluded,
        WorkflowRun,
        (
            "head_branch",
            "event",
            "conclusion",
            "run_started_at",
            "github_created_at",
            "github_updated_at",
        ),
        always_merge=("workflow_id",),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[WorkflowRun.run_id, WorkflowRun.run_attempt],
            set_={"head_sha": stmt.excluded.head_sha, "updated_at": func.now(), **merged},
        )
    )


async def run_workflow_id(session: AsyncSession, *, run_id: int) -> int | None:
    """The workflow a run belongs to, taken from whichever attempt knows it.

    Deliberately not scoped to one attempt: a run id determines its workflow, since
    an attempt is a re-execution of the same run. A re-run's own `workflow_run` event
    may not have arrived yet, and its jobs still belong to the workflow attempt 1
    already identified.

    Jobs read it from here rather than from their own payload, which carries no
    workflow id at all. Reading it back also keeps the foreign key safe: a value
    already stored on a run has proved that its `workflows` row exists.
    """
    return (
        await session.execute(
            select(WorkflowRun.workflow_id)
            .where(WorkflowRun.run_id == run_id, WorkflowRun.workflow_id.is_not(None))
            .limit(1)
        )
    ).scalar_one_or_none()


async def propagate_workflow_id(
    session: AsyncSession, *, run_id: int, workflow_id: int
) -> list[str]:
    """Fill the workflow id onto every attempt of a run, and its jobs, where unknown.

    Returns the names of the jobs that changed, because Signal B cannot group a job
    whose workflow is unknown — those are exactly the jobs now worth re-evaluating.
    """
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.run_id == run_id, WorkflowRun.workflow_id.is_(None))
        .values(workflow_id=workflow_id, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )
    names = (
        await session.execute(
            update(Job)
            .where(Job.run_id == run_id, Job.workflow_id.is_(None))
            .values(workflow_id=workflow_id, updated_at=func.now())
            .returning(Job.name)
            .execution_options(synchronize_session=False)
        )
    ).scalars()
    return sorted(set(names))


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
        # Zero must be stored as zero, not as "unknown": steps planned and none
        # finished is how a dead runner is told from a test failure (SPEC §2).
        completed_step_count=(
            sum(1 for s in steps if s.get("status") == "completed") if steps else None
        ),
    )
    merged = _never_regress(
        stmt.excluded,
        Job,
        (
            "conclusion",
            "started_at",
            "completed_at",
            "runner_name",
            "runner_labels",
            "step_count",
            "completed_step_count",
        ),
        always_merge=("workflow_id",),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Job.id],
            set_={"updated_at": func.now(), **merged},
        )
    )
