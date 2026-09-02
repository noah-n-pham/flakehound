"""The one read endpoint with no authentication at all (SPEC §8, §8b).

`/healthz` and the webhook are the other two unauthenticated paths, and the webhook
authenticates by HMAC signature. This router therefore has **no auth dependency by
design**, which is the opposite of every other router here — so what keeps it safe is
the query, not the caller: `public_flaky_jobs()` joins `repositories` on
`private = false` and takes no repo id, so there is nothing a caller can say that
widens what it returns.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.stats import public_flaky_jobs

router = APIRouter(prefix="/public")

SessionDep = Annotated[AsyncSession, Depends(session_scope)]


class PublicFlakyJob(BaseModel):
    """One row of the public board. Names its repo, because the board spans repos."""

    repo_id: int
    repo_full_name: str
    workflow_id: int | None
    job_name: str
    opportunities: int
    failures: int
    flakes: int
    last_flake_at: datetime | None
    flake_rate: float | None
    wilson_lower: float | None
    wilson_upper: float | None


@router.get("/flaky")
async def flaky(
    session: SessionDep,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[PublicFlakyJob]:
    """The flakiest jobs across every public repo, ranked by the Wilson lower bound.

    Ranking by the lower bound matters more here than on a private board: this one
    spans repos, so the "one flake in two runs" row would otherwise outrank a job
    with real evidence behind it, in public.
    """
    board = await public_flaky_jobs(session, window_days=window_days, limit=limit)
    return [
        PublicFlakyJob(
            repo_id=job.repo_id,
            repo_full_name=job.repo_full_name,
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
