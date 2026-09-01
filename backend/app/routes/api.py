"""Authenticated read endpoints. The browser never calls these — Next.js does."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_internal_token
from app.db import session_scope
from app.models import Job, Repository
from app.stats import flaky_jobs

router = APIRouter(prefix="/api", dependencies=[Depends(require_internal_token)])

SessionDep = Annotated[AsyncSession, Depends(session_scope)]


class RepoSummary(BaseModel):
    id: int
    full_name: str
    private: bool
    active: bool
    job_count: int
    last_job_at: datetime | None


class JobRow(BaseModel):
    id: int
    name: str
    run_id: int
    run_attempt: int
    head_sha: str
    status: str | None
    conclusion: str | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: float | None


class FlakyJob(BaseModel):
    """One leaderboard row. All three statistics are null at zero opportunities."""

    workflow_id: int | None
    job_name: str
    opportunities: int
    failures: int
    flakes: int
    last_flake_at: datetime | None
    flake_rate: float | None
    wilson_lower: float | None
    wilson_upper: float | None


async def _require_repo(session: AsyncSession, repo_id: int) -> None:
    exists = (
        await session.execute(select(Repository.id).where(Repository.id == repo_id))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown repo")


@router.get("/repos")
async def list_repos(session: SessionDep) -> list[RepoSummary]:
    rows = (
        await session.execute(
            select(
                Repository.id,
                Repository.full_name,
                Repository.private,
                Repository.active,
                func.count(Job.id).label("job_count"),
                func.max(Job.completed_at).label("last_job_at"),
            )
            .outerjoin(Job, Job.repo_id == Repository.id)
            .group_by(Repository.id)
            .order_by(Repository.full_name)
        )
    ).all()
    return [RepoSummary.model_validate(row._mapping) for row in rows]


@router.get("/repos/{repo_id}/jobs")
async def list_jobs(
    session: SessionDep,
    repo_id: Annotated[int, Path(description="GitHub's repo id")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[JobRow]:
    """Most recent job executions for one repo, newest first.

    Raw facts rather than the rollup, because these are individual executions
    rather than an aggregate — SPEC §8's rollup rule applies to the leaderboard
    and minutes endpoints that Section E adds.
    """
    await _require_repo(session, repo_id)

    jobs = (
        (
            await session.execute(
                select(Job)
                .where(Job.repo_id == repo_id)
                .order_by(Job.started_at.desc().nullslast(), Job.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        JobRow(
            id=job.id,
            name=job.name,
            run_id=job.run_id,
            run_attempt=job.run_attempt,
            head_sha=job.head_sha,
            status=job.status,
            conclusion=job.conclusion,
            started_at=job.started_at,
            completed_at=job.completed_at,
            duration_seconds=(
                (job.completed_at - job.started_at).total_seconds()
                if job.started_at and job.completed_at
                else None
            ),
        )
        for job in jobs
    ]


@router.get("/repos/{repo_id}/flaky")
async def list_flaky_jobs(
    session: SessionDep,
    repo_id: Annotated[int, Path(description="GitHub's repo id")],
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[FlakyJob]:
    """The flake leaderboard for one repo, ranked by the Wilson interval's lower bound.

    Both bounds and the point estimate are returned rather than the rank key alone,
    because the interval's width is what tells a reader how much to trust the rate —
    a job seen three times is not the same claim as a job seen three hundred.
    """
    await _require_repo(session, repo_id)

    board = await flaky_jobs(session, repo_id=repo_id, window_days=window_days, limit=limit)
    return [
        FlakyJob(
            workflow_id=job.workflow_id,
            job_name=job.job_name,
            opportunities=job.opportunities,
            failures=job.failures,
            flakes=job.flakes,
            last_flake_at=job.last_flake_at,
            flake_rate=job.interval.rate if job.interval else None,
            wilson_lower=job.interval.lower if job.interval else None,
            wilson_upper=job.interval.upper if job.interval else None,
        )
        for job in board
    ]
