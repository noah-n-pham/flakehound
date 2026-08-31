"""Webhook payload builders shaped like GitHub's real ones.

Only the fields the product reads are present; a real delivery carries far more.
Note what is *absent* from `workflow_job`: there is no `workflow_id`, which is
why a job's run row has to be stubbed before the job can be stored (D-005).
"""

from typing import Any

INSTALLATION_ID = 51_000_001
REPO_ID = 62_000_002
WORKFLOW_ID = 73_000_003
RUN_ID = 84_000_004
JOB_ID = 95_000_005
SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"


def repository(private: bool = False) -> dict[str, Any]:
    return {
        "id": REPO_ID,
        "name": "ci-insights",
        "full_name": "khoi/ci-insights",
        "private": private,
        "default_branch": "main",
        "owner": {"id": 41_000_000, "login": "khoi", "type": "User"},
    }


def workflow_job(
    *,
    job_id: int = JOB_ID,
    run_id: int = RUN_ID,
    run_attempt: int = 1,
    name: str = "test (ubuntu-latest, 3.12)",
    conclusion: str | None = "success",
    status: str = "completed",
    head_sha: str = SHA,
    completed_steps: int = 3,
) -> dict[str, Any]:
    return {
        "action": status,
        "workflow_job": {
            "id": job_id,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "workflow_name": "CI",
            "head_branch": "main",
            "head_sha": head_sha,
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "started_at": "2026-08-31T14:00:00Z",
            "completed_at": "2026-08-31T14:04:12Z",
            "runner_name": "GitHub Actions 2",
            "labels": ["ubuntu-latest"],
            "steps": [
                {"name": f"step {i}", "status": "completed", "conclusion": "success"}
                for i in range(completed_steps)
            ],
        },
        "repository": repository(),
        "installation": {"id": INSTALLATION_ID},
        "sender": {"id": 41_000_000, "login": "khoi"},
    }


def workflow_run(
    *,
    run_id: int = RUN_ID,
    run_attempt: int = 1,
    conclusion: str | None = "success",
    head_sha: str = SHA,
) -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "run_attempt": run_attempt,
            "workflow_id": WORKFLOW_ID,
            "head_branch": "main",
            "head_sha": head_sha,
            "event": "push",
            "status": "completed",
            "conclusion": conclusion,
            "run_started_at": "2026-08-31T14:00:00Z",
            "created_at": "2026-08-31T13:59:55Z",
            "updated_at": "2026-08-31T14:05:00Z",
        },
        "workflow": {"id": WORKFLOW_ID, "name": "CI", "path": ".github/workflows/ci.yml"},
        "repository": repository(),
        "installation": {"id": INSTALLATION_ID},
    }
