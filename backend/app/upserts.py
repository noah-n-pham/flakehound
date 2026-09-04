"""Idempotency layer 2: every fact write is an upsert on GitHub's own ids.

GitHub's ids are immutable and authoritative, so replaying any payload converges
to identical state. Enrichment fields use COALESCE(EXCLUDED, existing) wherever a
later payload may know less than an earlier one: a `workflow_job` event carries
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

    GitHub sends three deliveries per job and per run (queued, in progress, completed),
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
    identity_columns: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Merge a payload only when it is at least as advanced as the row already stored.

    A payload that is *behind* what we have contributes nothing: an `in_progress` body
    carries a null conclusion and a partial step count, and applying it after the
    `completed` body would erase a terminal fact. That is not a hypothetical: observed
    against live deliveries, it turned a successful job run into one with no conclusion,
    silently removing it from Signal A's opportunities.

    `identity_columns` are exempt from the ranking because they carry identity rather than
    progress, but they are **first-write-wins** rather than last: a run's workflow cannot
    change, so once the value is known it is never replaced. Last-write-wins would leave
    even an immutable field order-dependent if two payloads ever disagreed.
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
            column: func.coalesce(getattr(table, column), excluded[column])
            for column in identity_columns
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


async def set_installation_lifecycle(
    session: AsyncSession,
    *,
    installation_id: int,
    suspended_at: datetime | None,
    deleted_at: datetime | None,
) -> None:
    """Write both lifecycle timestamps outright.

    Both are written every time rather than merged, because the caller derives
    them from the event's action and always knows both. That makes the write
    last-action-wins: GitHub's installation payload carries no version to rank
    two of them by, the way a job's `status` ranks job payloads.
    """
    await session.execute(
        update(Installation)
        .where(Installation.id == installation_id)
        .values(suspended_at=suspended_at, deleted_at=deleted_at, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def upsert_repository(
    session: AsyncSession,
    *,
    installation_id: int | None,
    repository: dict[str, Any],
    active: bool | None = None,
    source: str = "installed",
) -> int:
    """Upsert one repo. `active=None` leaves an existing row's flag untouched.

    Only the installation events know whether a repo is still installed, so they
    pass the flag explicitly. A `workflow_job` for a repo that was removed from
    the install must not quietly re-activate it. That decision belongs to
    `installation_repositories`, and a late job event is not evidence of it.

    **`source` moves in one direction only, and the asymmetry is the point.** An
    installed write claims the row: it sets `source = 'installed'` and the installation
    id, which is exactly SPEC §4's "a repo that later installs the App becomes installed
    in place, under the same GitHub repo id". The crawled history stays, keyed on the
    same GitHub repo id. An observed write never claims it: on conflict it leaves both
    columns alone, so crawling a repo that already installed the App cannot demote it to
    a repo nobody owns. Without that asymmetry the webhook and the crawl would fight over
    every dogfooded repo, and the check constraint would start rejecting writes.
    """
    if source == "observed":
        # These two are the check constraint restated, raised here so the failure names
        # the caller's mistake instead of surfacing as an IntegrityError from Postgres.
        if installation_id is not None:
            raise ValueError("an observed repository has no installation")
        if bool(repository.get("private", True)):
            raise ValueError("an observed repository must be public")
    elif installation_id is None:
        raise ValueError("an installed repository needs an installation")

    owner = repository.get("owner") or {}
    full_name = repository.get("full_name") or ""
    stmt = insert(Repository).values(
        id=repository["id"],
        installation_id=installation_id,
        source=source,
        owner=owner.get("login") or full_name.split("/")[0],
        name=repository.get("name") or full_name.rpartition("/")[2],
        full_name=full_name,
        private=bool(repository.get("private", True)),
        active=True if active is None else active,
        default_branch=repository.get("default_branch"),
    )
    claims_the_row = source == "installed"
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Repository.id],
            set_={
                "source": stmt.excluded.source if claims_the_row else Repository.source,
                "installation_id": (
                    stmt.excluded.installation_id
                    if claims_the_row
                    else Repository.installation_id
                ),
                "owner": stmt.excluded.owner,
                "name": stmt.excluded.name,
                "full_name": stmt.excluded.full_name,
                "private": stmt.excluded.private,
                "active": Repository.active if active is None else stmt.excluded.active,
                "default_branch": func.coalesce(
                    stmt.excluded.default_branch, Repository.default_branch
                ),
                "updated_at": func.now(),
            },
        )
    )
    return int(repository["id"])


async def set_repositories_active(
    session: AsyncSession, *, repo_ids: list[int], active: bool
) -> list[int]:
    """Flip the active flag on repos we already know about.

    Nothing is deleted. A removed repo keeps its jobs, runs, and flake events.
    They are still true, the repo is simply no longer being watched, and rows
    elsewhere reference it by foreign key.
    """
    if not repo_ids:
        return []
    changed = (
        await session.execute(
            update(Repository)
            .where(Repository.id.in_(repo_ids), Repository.active.is_not(active))
            .values(active=active, updated_at=func.now())
            .returning(Repository.id)
            .execution_options(synchronize_session=False)
        )
    ).scalars()
    return sorted(changed)


async def set_installation_repositories_active(
    session: AsyncSession, *, installation_id: int, active: bool
) -> list[int]:
    """The same, for every repo of one installation: an install or uninstall."""
    changed = (
        await session.execute(
            update(Repository)
            .where(
                Repository.installation_id == installation_id,
                Repository.active.is_not(active),
            )
            .values(active=active, updated_at=func.now())
            .returning(Repository.id)
            .execution_options(synchronize_session=False)
        )
    ).scalars()
    return sorted(changed)


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
    """Upsert one run attempt. Also used to stub a run from a job payload.

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
        identity_columns=("workflow_id",),
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
    whose workflow is unknown. Those are exactly the jobs now worth re-evaluating.
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
        # finished is how a dead runner is told from a test failure.
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
        identity_columns=("workflow_id",),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Job.id],
            set_={"updated_at": func.now(), **merged},
        )
    )
