"""Read the rollup out of production and check it against the facts it summarises.

Run with an ECS command override; the output lands in CloudWatch. It is the
production twin of `tests/test_rollup.py::test_the_rollup_agrees_with_the_raw_facts`:
the same leaderboard computed from `job_stats_daily` and from the job rows, printed
side by side so a difference is visible rather than argued about.
"""

import asyncio
import json
import sys

from sqlalchemy import select

from app.db import dispose_engine, get_sessionmaker
from app.models import JobStatsDaily
from app.stats import flaky_jobs, flaky_jobs_from_facts


def _board(board) -> list[dict]:
    return [
        {
            "job": job.job_name,
            "wf": job.workflow_id,
            "opps": job.opportunities,
            "failures": job.failures,
            "flakes": job.flakes,
            "lower": round(job.interval.lower, 4) if job.interval else None,
            "upper": round(job.interval.upper, 4) if job.interval else None,
        }
        for job in board
    ]


async def main(repo_id: int) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(JobStatsDaily)
                    .where(JobStatsDaily.repo_id == repo_id)
                    .order_by(JobStatsDaily.day, JobStatsDaily.job_name)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            print(
                json.dumps(
                    {
                        "day": str(row.day),
                        "wf": row.workflow_id,
                        "job": row.job_name,
                        "runs": row.runs,
                        "opps": row.opportunities,
                        "failures": row.failures,
                        "flakes": row.flakes,
                        "p50": float(row.duration_p50_seconds or 0),
                        "p95": float(row.duration_p95_seconds or 0),
                        "total": float(row.duration_total_seconds or 0),
                        "last_flake_at": str(row.last_flake_at),
                    }
                ),
                flush=True,
            )

        from_rollup = await flaky_jobs(session, repo_id=repo_id, window_days=90)
        from_facts = await flaky_jobs_from_facts(session, repo_id=repo_id, window_days=90)
        print(json.dumps({"rollup": _board(from_rollup)}), flush=True)
        print(json.dumps({"facts": _board(from_facts)}), flush=True)
        print(json.dumps({"agree": _board(from_rollup) == _board(from_facts)}), flush=True)

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
