"""Eligibility for the observational public board.

The assertion this file exists for is the one in
`test_the_verdict_cannot_see_an_outcome`: admission is decided from public metadata
and *cannot* read a flake rate, because the type it is given has nowhere to put one.
Everything else here is one test per criterion, so a criterion cannot be quietly
loosened without a test going red.
"""

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.observe import (
    MIN_COMPLETED_RUNS,
    REQUESTS_PER_CANDIDATE,
    CandidateFacts,
    assess,
    facts_from_payloads,
    fetch_candidate_facts,
    screen,
)
from tests.conftest import INSTALLATION_ID

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
FULL_NAME = "astral-sh/uv"
REPO_URL = f"https://api.github.com/repos/{FULL_NAME}"


def facts(**overrides) -> CandidateFacts:
    """An eligible candidate, which each test then breaks in exactly one way."""
    base = CandidateFacts(
        repo_id=699_532_645,
        full_name=FULL_NAME,
        private=False,
        archived=False,
        disabled=False,
        fork=False,
        is_template=False,
        pushed_at=NOW - timedelta(hours=2),
        default_branch="main",
        stargazers_count=420,
        active_workflows=3,
        completed_runs=250,
        code_triggered_runs=80,
    )
    return replace(base, **overrides)


# --------------------------------------------------------------------------- #
# The rule that protects the board's credibility
# --------------------------------------------------------------------------- #


def test_the_verdict_cannot_see_an_outcome():
    """**Admission cannot be biased by a preliminary crawl, structurally.**

    A board whose repositories were picked because a trial crawl found them flaky
    would report a selection effect as a finding, and nothing on the finished page
    would reveal it. So the guard is the input type: if no field can carry a flake
    rate, a conclusion, or a job outcome, then `assess()` cannot weigh one.
    """
    names = {field.name for field in fields(CandidateFacts)}

    assert not names & {
        "flakes",
        "flake_rate",
        "flake_events",
        "opportunities",
        "failures",
        "conclusion",
        "conclusions",
        "wilson_lower",
    }
    # And the two counts it *does* carry are volume, not outcome: they count runs
    # that finished, never runs that failed.
    assert {"completed_runs", "code_triggered_runs"} <= names


def test_stars_are_a_discovery_aid_and_never_an_eligibility_rule():
    """The brief gives three star bands to search in, not a threshold to pass."""
    assert assess(facts(stargazers_count=0), now=NOW).eligible
    assert assess(facts(stargazers_count=89_000), now=NOW).eligible


# --------------------------------------------------------------------------- #
# One test per criterion
# --------------------------------------------------------------------------- #


def test_a_public_active_repo_with_real_ci_is_eligible():
    verdict = assess(facts(), now=NOW)

    assert verdict.eligible
    assert verdict.reasons == ()


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"private": True}, "private"),
        ({"archived": True}, "archived"),
        ({"disabled": True}, "disabled"),
        ({"fork": True}, "fork"),
        ({"is_template": True}, "template"),
        ({"pushed_at": None}, "never_pushed"),
        ({"pushed_at": NOW - timedelta(days=31)}, "stale"),
        ({"active_workflows": 0}, "no_active_workflows"),
        ({"completed_runs": MIN_COMPLETED_RUNS - 1}, "too_little_history"),
        ({"code_triggered_runs": 0}, "no_code_triggered_runs"),
    ],
)
def test_each_criterion_rejects_on_its_own(override, reason):
    verdict = assess(facts(**override), now=NOW)

    assert not verdict.eligible
    assert verdict.reasons == (reason,)


def test_the_history_threshold_is_inclusive():
    """Exactly the minimum is enough; the ranking is what judges it after that."""
    assert assess(facts(completed_runs=MIN_COMPLETED_RUNS), now=NOW).eligible


def test_a_push_within_the_window_is_fresh_enough():
    assert assess(facts(pushed_at=NOW - timedelta(days=29, hours=23)), now=NOW).eligible


def test_every_reason_is_reported_not_just_the_first():
    verdict = assess(
        facts(archived=True, fork=True, active_workflows=0, completed_runs=2), now=NOW
    )

    assert verdict.reasons == (
        "archived",
        "fork",
        "no_active_workflows",
        "too_little_history",
    )


def test_a_cron_only_repo_is_not_ci_reacting_to_code():
    """A nightly schedule produces runs without anybody changing anything, so
    re-run recovery and same-commit disagreement have nothing to compare."""
    verdict = assess(facts(completed_runs=400, code_triggered_runs=0), now=NOW)

    assert verdict.reasons == ("no_code_triggered_runs",)


# --------------------------------------------------------------------------- #
# Reading the facts off the three payloads
# --------------------------------------------------------------------------- #


def test_the_facts_come_off_the_three_payloads_verbatim():
    assembled = facts_from_payloads(
        {
            "id": 699_532_645,
            "full_name": FULL_NAME,
            "private": False,
            "archived": False,
            "fork": False,
            "is_template": False,
            "pushed_at": "2026-09-03T09:12:41Z",
            "default_branch": "main",
            "stargazers_count": 89_397,
        },
        {
            "total_count": 3,
            "workflows": [
                {"id": 1, "state": "active"},
                {"id": 2, "state": "active"},
                {"id": 3, "state": "disabled_inactivity"},
            ],
        },
        {
            "total_count": 812,
            "workflow_runs": [
                {"id": 10, "event": "push"},
                {"id": 11, "event": "pull_request"},
                {"id": 12, "event": "schedule"},
                {"id": 13, "event": "workflow_dispatch"},
            ],
        },
    )

    assert assembled.repo_id == 699_532_645
    assert assembled.stargazers_count == 89_397
    # A disabled workflow is not an active one.
    assert assembled.active_workflows == 2
    # The listing's total, not the page length.
    assert assembled.completed_runs == 812
    # Only push and pull_request count as code-triggered.
    assert assembled.code_triggered_runs == 2
    assert assembled.pushed_at == datetime(2026, 9, 3, 9, 12, 41, tzinfo=UTC)


def test_a_payload_missing_its_flags_is_read_as_private_rather_than_public():
    """The safe default when GitHub says nothing: assume the stricter answer.

    `upsert_repository` already defaults `private` to True for the same reason, and
    the two must not disagree — a repo read as public by mistake is the one mistake
    this whole direction cannot make.
    """
    assembled = facts_from_payloads({"id": 1, "full_name": "a/b"}, {}, {})

    assert assembled.private is True
    assert assess(assembled, now=NOW).reasons[0] == "private"


# --------------------------------------------------------------------------- #
# The three requests, stubbed with respx
# --------------------------------------------------------------------------- #


def _stub(router: respx.Router, *, runs_total: int = 250) -> None:
    router.get(REPO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 699_532_645,
                "full_name": FULL_NAME,
                "private": False,
                "archived": False,
                "fork": False,
                "is_template": False,
                "pushed_at": "2026-09-03T09:12:41Z",
                "default_branch": "main",
                "stargazers_count": 420,
            },
        )
    )
    router.get(f"{REPO_URL}/actions/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": [{"id": 1, "state": "active"}]})
    )
    router.get(f"{REPO_URL}/actions/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": runs_total,
                "workflow_runs": [{"id": 10, "event": "push"}],
            },
        )
    )


async def test_admission_costs_exactly_three_requests(app_credentials, token_route):
    """The budget a discovery pass has to plan against, asserted rather than assumed."""
    _stub(token_route)

    assembled = await fetch_candidate_facts(
        FULL_NAME, installation_id=INSTALLATION_ID, now=NOW
    )

    assert assembled is not None
    github_calls = [
        call for call in token_route.calls if "access_tokens" not in str(call.request.url)
    ]
    assert len(github_calls) == REQUESTS_PER_CANDIDATE


async def test_the_runs_query_asks_only_for_completed_runs_in_the_window(
    app_credentials, token_route
):
    _stub(token_route)

    await fetch_candidate_facts(FULL_NAME, installation_id=INSTALLATION_ID, now=NOW)

    runs = next(
        call for call in token_route.calls if "actions/runs" in str(call.request.url)
    )
    params = runs.request.url.params
    assert params["status"] == "completed"
    assert params["created"] == ">=2026-08-04"
    assert params["per_page"] == "100"


async def test_a_missing_repo_is_none_rather_than_an_error(app_credentials, token_route):
    """Renamed, deleted, or newly private. Ordinary, and a crawl must survive it."""
    token_route.get(REPO_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

    assert (
        await fetch_candidate_facts(FULL_NAME, installation_id=INSTALLATION_ID, now=NOW)
    ) is None


async def test_screening_an_unreadable_repo_says_so_instead_of_raising(
    app_credentials, token_route
):
    token_route.get(REPO_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

    assembled, verdict = await screen(
        FULL_NAME, installation_id=INSTALLATION_ID, now=NOW
    )

    assert assembled is None
    assert (verdict.eligible, verdict.reasons) == (False, ("unreadable",))


async def test_screening_returns_both_the_facts_and_the_verdict(app_credentials, token_route):
    _stub(token_route)

    assembled, verdict = await screen(
        FULL_NAME, installation_id=INSTALLATION_ID, now=NOW
    )

    assert verdict.eligible
    assert assembled.full_name == FULL_NAME
    assert assembled.completed_runs == 250


async def test_a_repo_with_too_little_history_is_screened_out_not_crawled(
    app_credentials, token_route
):
    """The point of screening: the decision costs three requests, not a crawl."""
    _stub(token_route, runs_total=4)

    _, verdict = await screen(FULL_NAME, installation_id=INSTALLATION_ID, now=NOW)

    assert verdict.reasons == ("too_little_history",)
