"""Webhook payload builders shaped like GitHub's real ones.

Only the fields the product reads are present; a real delivery carries far more.
Note what is *absent* from `workflow_job`: there is no `workflow_id`, which is
why a job's run row has to be stubbed before the job can be stored.
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
        "name": "flakehound",
        "full_name": "khoi/flakehound",
        "private": private,
        "default_branch": "main",
        "owner": {"id": 41_000_000, "login": "khoi", "type": "User"},
    }


def minimal_repository(repo_id: int = REPO_ID, name: str = "flakehound") -> dict[str, Any]:
    """What an installation event carries, verified against a real delivery.

    Production's `repositories_added` held exactly these five keys:
    `{"id": 1083370024, "name": "form-check", "node_id": "R_kgDOQJLqKA",
    "private": false, "full_name": "noah-n-pham/form-check"}`. No owner object
    and no default branch, which is why `upsert_repository` falls back to
    splitting `full_name` and leaves the branch unknown.
    """
    return {
        "id": repo_id,
        "name": name,
        "node_id": f"R_{repo_id}",
        "full_name": f"khoi/{name}",
        "private": False,
    }


def installation_event(
    *,
    action: str = "created",
    installation_id: int = INSTALLATION_ID,
    account_login: str = "khoi",
    account_type: str = "User",
    suspended_at: str | None = None,
    repositories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """install, uninstall, suspend, unsuspend.

    The account block appears here and in no other event, which is what makes
    this the only handler that can fill in a stubbed installation row.
    """
    return {
        "action": action,
        "installation": {
            "id": installation_id,
            "account": {"id": 41_000_000, "login": account_login, "type": account_type},
            "repository_selection": "selected",
            "suspended_at": suspended_at,
            "created_at": "2026-08-01T10:00:00Z",
            "updated_at": "2026-08-31T10:00:00Z",
        },
        "repositories": (
            [minimal_repository()] if repositories is None else repositories
        ),
        "sender": {"id": 41_000_000, "login": account_login},
    }


def installation_repositories_event(
    *,
    action: str = "added",
    installation_id: int = INSTALLATION_ID,
    added: list[dict[str, Any]] | None = None,
    removed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "installation": {
            "id": installation_id,
            "account": {"id": 41_000_000, "login": "khoi", "type": "User"},
            "repository_selection": "selected",
        },
        "repository_selection": "selected",
        "repositories_added": added or [],
        "repositories_removed": removed or [],
        "sender": {"id": 41_000_000, "login": "khoi"},
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
    total_steps: int | None = None,
    started_at: str = "2026-08-31T14:00:00Z",
    completed_at: str | None = "2026-08-31T14:04:12Z",
) -> dict[str, Any]:
    """`total_steps` above `completed_steps` is how a dead runner looks: steps were
    planned and some never started.

    The timestamps are parameters because the rollup buckets by the UTC day a job
    completed and aggregates the duration between them, so its tests need job runs
    that finish on different days and take different lengths of time.
    """
    planned = completed_steps if total_steps is None else total_steps
    steps = [
        {
            "name": f"step {i}",
            "status": "completed" if i < completed_steps else "queued",
            "conclusion": "success" if i < completed_steps else None,
        }
        for i in range(planned)
    ]
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
            "started_at": started_at,
            "completed_at": completed_at,
            "runner_name": "GitHub Actions 2",
            "labels": ["ubuntu-latest"],
            "steps": steps,
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
    workflow_id: int = WORKFLOW_ID,
    workflow_name: str = "CI",
    status: str = "completed",
) -> dict[str, Any]:
    """One push produces three of these: requested, in progress, and completed."""
    action = {"queued": "requested", "in_progress": "in_progress"}.get(status, "completed")
    return {
        "action": action,
        "workflow_run": {
            "id": run_id,
            "run_attempt": run_attempt,
            "workflow_id": workflow_id,
            "head_branch": "main",
            "head_sha": head_sha,
            "event": "push",
            "status": status,
            "conclusion": conclusion,
            "run_started_at": "2026-08-31T14:00:00Z",
            "created_at": "2026-08-31T13:59:55Z",
            "updated_at": "2026-08-31T14:05:00Z",
        },
        "workflow": {
            "id": workflow_id,
            "name": workflow_name,
            "path": f".github/workflows/{workflow_name}.yml",
        },
        "repository": repository(),
        "installation": {"id": INSTALLATION_ID},
    }
