"""Which public repositories the observational board may watch, and why.

The public board ranks repositories that never installed the App, so *choosing* them
is part of the product and has to be as defensible as the detection. Two rules govern
this module and neither is negotiable.

**Admission is decided from public metadata alone, before anything is crawled.** The
facts in `CandidateFacts` are the whole input: what GitHub says about the repository,
how many completed runs it has, and what triggered them. No flake rate, no job
outcome, and nothing a crawl produced can appear there, which is what stops the board
from being a list of repositories chosen *because* a preliminary crawl found them
flaky. That selection effect would be invisible in the finished page and would make
every number on it meaningless, so the defence against it is structural: the function
that admits a repository cannot see the data that would bias it.

**Admission is permanent and publication is uniform.** Once a repository is admitted it
stays admitted, whatever its rate turns out to be, and its jobs reach the board only
through the same Wilson ranking every installed repo goes through. There is no step
where a disappointing repository is quietly dropped.

Three requests per candidate, and that is the whole admission cost: the repository, its
workflows, and one page of its recent completed runs.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.github import api_request
from app.logging import get_logger
from app.upserts import parse_timestamp

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# The criteria, as numbers rather than prose
# --------------------------------------------------------------------------- #

# How far back "recent" reaches, for both the push and the run count below. It
# matches the board's own 30-day window: a repository whose evidence is older than
# the window the page shows could never put a row on it.
WINDOW_DAYS = 30
# A repository nobody has pushed to inside the window is not producing CI to watch.
PUSHED_WITHIN_DAYS = 30
# Enough completed runs for the Wilson lower bound to rise above noise at all. Below
# this the crawl spends requests to publish nothing, because the ranking correctly
# refuses to trust a handful of observations.
MIN_COMPLETED_RUNS = 30
# At least one recent run triggered by code rather than by a clock or a button. This
# is what separates real CI from a nightly cron or a manual deploy.
CODE_EVENTS = ("push", "pull_request")
MIN_CODE_TRIGGERED_RUNS = 1
# One page of runs is all admission reads. 100 is GitHub's maximum and it is plenty:
# every fact below is a threshold well under it.
RUNS_PAGE_SIZE = 100
# Requests spent deciding about one candidate. Stated so a discovery pass can budget
# against the 5,000/hour ceiling rather than discovering it.
REQUESTS_PER_CANDIDATE = 3


@dataclass(frozen=True)
class CandidateFacts:
    """Public metadata about one repository. **Deliberately contains no outcomes.**

    `stargazers_count` is carried because stars are how candidates are *discovered*
    across the three popularity bands, never because they make a repository eligible.
    Nothing in `assess()` reads it.
    """

    repo_id: int
    full_name: str
    private: bool
    archived: bool
    disabled: bool
    fork: bool
    is_template: bool
    pushed_at: datetime | None
    default_branch: str | None
    stargazers_count: int
    active_workflows: int
    completed_runs: int
    code_triggered_runs: int


@dataclass(frozen=True)
class Verdict:
    """Eligible, or every reason it is not: not merely the first one.

    All the reasons, because a discovery pass that prints "stale" and stops teaches
    less than one that says "stale, no workflows, 3 runs", and because a criterion
    that never fires is a criterion worth deleting.
    """

    full_name: str
    eligible: bool
    reasons: tuple[str, ...]


def assess(facts: CandidateFacts, *, now: datetime | None = None) -> Verdict:
    """Apply the criteria above. The only input is public metadata."""
    now = now or datetime.now(UTC)
    reasons: list[str] = []

    # Public first, because it is the one criterion that is also a database
    # constraint: `ck_repositories_source_installation` refuses to store an observed
    # repo that is private, so admitting one would fail at the insert anyway.
    if facts.private:
        reasons.append("private")
    if facts.archived:
        reasons.append("archived")
    if facts.disabled:
        reasons.append("disabled")
    # A fork's Actions history is mostly its upstream's, and the throwaway forks the
    # brief warns about are overwhelmingly forks.
    if facts.fork:
        reasons.append("fork")
    if facts.is_template:
        reasons.append("template")

    if facts.pushed_at is None:
        reasons.append("never_pushed")
    elif facts.pushed_at < now - timedelta(days=PUSHED_WITHIN_DAYS):
        reasons.append("stale")

    if facts.active_workflows < 1:
        reasons.append("no_active_workflows")
    if facts.completed_runs < MIN_COMPLETED_RUNS:
        reasons.append("too_little_history")
    if facts.code_triggered_runs < MIN_CODE_TRIGGERED_RUNS:
        reasons.append("no_code_triggered_runs")

    return Verdict(full_name=facts.full_name, eligible=not reasons, reasons=tuple(reasons))


# --------------------------------------------------------------------------- #
# Reading the facts off GitHub
# --------------------------------------------------------------------------- #


def _window_filter(now: datetime) -> str:
    """GitHub's `created` filter, open-ended at the recent end."""
    return f">={(now - timedelta(days=WINDOW_DAYS)).date().isoformat()}"


def facts_from_payloads(
    repository: dict[str, Any],
    workflows: dict[str, Any],
    runs: dict[str, Any],
) -> CandidateFacts:
    """Assemble the facts from the three responses. Pure, so it is testable alone."""
    listed = runs.get("workflow_runs") or []
    return CandidateFacts(
        repo_id=int(repository["id"]),
        full_name=repository["full_name"],
        private=bool(repository.get("private", True)),
        archived=bool(repository.get("archived", False)),
        disabled=bool(repository.get("disabled", False)),
        fork=bool(repository.get("fork", False)),
        is_template=bool(repository.get("is_template", False)),
        pushed_at=parse_timestamp(repository.get("pushed_at")),
        default_branch=repository.get("default_branch"),
        stargazers_count=int(repository.get("stargazers_count") or 0),
        # `state` is "active" or "disabled_manually"/"disabled_inactivity".
        active_workflows=sum(
            1
            for workflow in (workflows.get("workflows") or [])
            if workflow.get("state") == "active"
        ),
        # The listing's own total, which is the count in the window rather than the
        # page length. The page is capped at 100 and the threshold is well below it.
        completed_runs=int(runs.get("total_count") or 0),
        code_triggered_runs=sum(1 for run in listed if run.get("event") in CODE_EVENTS),
    )


async def fetch_candidate_facts(
    full_name: str,
    *,
    installation_id: int,
    now: datetime | None = None,
) -> CandidateFacts | None:
    """The three reads that admission needs. None when the repo is gone or private.

    Every call goes through `api_request`, so the observation identity's token buys
    them and its bucket paces them: the same limiter the installed backfill uses,
    which is why a crawl must never outrank real work (D-046).

    A 404 is the ordinary answer for a renamed, deleted, or newly-private repository,
    and for anything the token may not read. It is data, not a failure.
    """
    now = now or datetime.now(UTC)

    repository = await api_request(installation_id, "GET", f"/repos/{full_name}")
    if repository.status_code == 404:
        log.info("observe.candidate_unreadable", full_name=full_name)
        return None
    repository.raise_for_status()

    workflows = await api_request(
        installation_id,
        "GET",
        f"/repos/{full_name}/actions/workflows",
        params={"per_page": RUNS_PAGE_SIZE},
    )
    workflows.raise_for_status()

    runs = await api_request(
        installation_id,
        "GET",
        f"/repos/{full_name}/actions/runs",
        params={
            "status": "completed",
            "created": _window_filter(now),
            "per_page": RUNS_PAGE_SIZE,
            "exclude_pull_requests": "false",
        },
    )
    runs.raise_for_status()

    return facts_from_payloads(repository.json(), workflows.json(), runs.json())


async def screen(
    full_name: str,
    *,
    installation_id: int,
    now: datetime | None = None,
) -> tuple[CandidateFacts | None, Verdict]:
    """Fetch and assess one candidate, logging the verdict either way.

    The rejections are logged as deliberately as the admissions, because "how many
    candidates were looked at and why they failed" is the only honest answer to "how
    were these repositories chosen".
    """
    facts = await fetch_candidate_facts(
        full_name, installation_id=installation_id, now=now
    )
    if facts is None:
        return None, Verdict(full_name=full_name, eligible=False, reasons=("unreadable",))

    verdict = assess(facts, now=now)
    log.info(
        "observe.screened",
        full_name=facts.full_name,
        repo_id=facts.repo_id,
        eligible=verdict.eligible,
        reasons=list(verdict.reasons),
        stars=facts.stargazers_count,
        completed_runs=facts.completed_runs,
        code_triggered_runs=facts.code_triggered_runs,
        active_workflows=facts.active_workflows,
    )
    return facts, verdict


def observation_installation_id() -> int:
    """Whose token reads public repositories. Raises by name when unset.

    Never falls back to anonymous requests: those work but at 60/hour rather than
    5,000, so the fallback would not fail, it would merely be eighty times slower and
    look like a network problem instead of a missing setting.
    """
    installation_id = get_settings().observation_installation_id
    if installation_id is None:
        raise RuntimeError(
            "OBSERVATION_INSTALLATION_ID is not set: the observational board needs an "
            "installation whose token reads public repositories (D-046)"
        )
    return installation_id


async def _main(full_names: list[str]) -> None:
    installation_id = observation_installation_id()
    admitted: list[str] = []
    for full_name in full_names:
        _, verdict = await screen(full_name, installation_id=installation_id)
        if verdict.eligible:
            admitted.append(full_name)
    log.info(
        "observe.screening_complete",
        candidates=len(full_names),
        admitted=len(admitted),
        requests_spent=len(full_names) * REQUESTS_PER_CANDIDATE,
        eligible=admitted,
    )


def main() -> None:
    """`python -m app.observe owner/repo [owner/repo ...]`: screen, decide nothing else.

    Read-only and writes no rows: screening is a judgement about public metadata, and
    keeping it separate from the crawl is what makes the admission list auditable.
    """
    import argparse
    import asyncio

    from app.logging import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Screen public repos for the board.")
    parser.add_argument("full_names", nargs="+", metavar="owner/repo")
    args = parser.parse_args()
    asyncio.run(_main(args.full_names))


if __name__ == "__main__":
    main()
