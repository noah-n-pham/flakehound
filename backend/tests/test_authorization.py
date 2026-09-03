"""The authorized-repo filter — the boundary that matters most in this service.

Two repos exist in every test here, and the caller is authorized for exactly one of
them. Each test then asks a different way of getting at the other one: list it, read its
jobs, read its leaderboard, name it in the path while claiming a different set, send no
set at all. The interesting assertion is never "the right rows came back" — it is that
the second repo is absent, and that absence is indistinguishable from the repo not
existing.
"""

from typing import Any

from app.auth import AUTHORIZED_REPOS_HEADER
from app.rollup import rollup_repository
from tests.helpers import deliver
from tests.payloads import REPO_ID
from tests.test_api import auth
from tests.test_detection import OTHER_RUN_ID, OTHER_WORKFLOW_ID, RUN_ID, attempt, run_event
from tests.test_public import in_repo

OTHER_REPO_ID = REPO_ID + 1


def headers(*repo_ids: int) -> dict[str, str]:
    """The bearer token plus an authorized set. Both are always present in production."""
    return {**auth(), AUTHORIZED_REPOS_HEADER: ",".join(str(repo_id) for repo_id in repo_ids)}


async def seed_two_repos(session) -> None:
    """`khoi/flakehound` and `khoi/other`, each with a flaky job and a rollup."""
    await deliver(
        session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure"),
        attempt(2, "success"),
    )
    await deliver(
        session,
        *(
            in_repo(payload, repo_id=OTHER_REPO_ID, name="other", private=True)
            for payload in (
                run_event(run_id=OTHER_RUN_ID, workflow_id=OTHER_WORKFLOW_ID),
                attempt(1, "failure", run_id=OTHER_RUN_ID),
                attempt(2, "success", run_id=OTHER_RUN_ID),
            )
        ),
    )
    for repo_id in (REPO_ID, OTHER_REPO_ID):
        await rollup_repository(session, repo_id=repo_id)


def names(rows: list[dict[str, Any]]) -> list[str]:
    return [row["full_name"] for row in rows]


# --------------------------------------------------------------------------- #
# The repo list is the authorized set, not the table
# --------------------------------------------------------------------------- #


async def test_the_repo_list_holds_only_authorized_repos(client, db_session):
    await seed_two_repos(db_session)

    response = await client.get("/api/repos", headers=headers(REPO_ID))

    assert response.status_code == 200
    assert names(response.json()) == ["khoi/flakehound"]


async def test_authorizing_both_repos_returns_both(client, db_session):
    """The complement of the test above: the filter is the header, not a fixed rule."""
    await seed_two_repos(db_session)

    response = await client.get("/api/repos", headers=headers(REPO_ID, OTHER_REPO_ID))

    assert names(response.json()) == ["khoi/flakehound", "khoi/other"]


async def test_an_empty_authorized_set_returns_nothing_not_everything(client, db_session):
    """A user with no installations. The dangerous reading of "empty" is "unfiltered"."""
    await seed_two_repos(db_session)

    response = await client.get("/api/repos", headers=headers())

    assert response.status_code == 200
    assert response.json() == []


# --------------------------------------------------------------------------- #
# Naming an unauthorized repo directly
# --------------------------------------------------------------------------- #


async def test_another_repos_jobs_are_a_404(client, db_session):
    await seed_two_repos(db_session)

    response = await client.get(f"/api/repos/{OTHER_REPO_ID}/jobs", headers=headers(REPO_ID))

    assert response.status_code == 404


async def test_another_repos_leaderboard_is_a_404(client, db_session):
    await seed_two_repos(db_session)

    response = await client.get(f"/api/repos/{OTHER_REPO_ID}/flaky", headers=headers(REPO_ID))

    assert response.status_code == 404


async def test_an_unauthorized_repo_is_indistinguishable_from_a_missing_one(client, db_session):
    """404 rather than 403, deliberately: a 403 confirms the repo exists.

    Both the status and the body have to match, or the difference is still readable.
    """
    await seed_two_repos(db_session)

    unauthorized = await client.get(
        f"/api/repos/{OTHER_REPO_ID}/jobs", headers=headers(REPO_ID)
    )
    nonexistent = await client.get("/api/repos/999999999/jobs", headers=headers(REPO_ID))

    assert unauthorized.status_code == nonexistent.status_code == 404
    assert unauthorized.json() == nonexistent.json()


async def test_the_authorized_repo_still_works(client, db_session):
    """The filter has to deny the other repo without breaking the one that is allowed."""
    await seed_two_repos(db_session)

    jobs = await client.get(f"/api/repos/{REPO_ID}/jobs", headers=headers(REPO_ID))
    flaky = await client.get(
        f"/api/repos/{REPO_ID}/flaky", params={"window_days": 90}, headers=headers(REPO_ID)
    )

    assert jobs.status_code == 200
    assert len(jobs.json()) == 2
    assert flaky.status_code == 200
    assert [row["job_name"] for row in flaky.json()] == ["build and deploy"]


async def test_authorizing_only_the_other_repo_flips_which_one_is_visible(client, db_session):
    """Nothing about `REPO_ID` is privileged; the header is the whole story."""
    await seed_two_repos(db_session)

    mine = await client.get(f"/api/repos/{REPO_ID}/jobs", headers=headers(OTHER_REPO_ID))
    theirs = await client.get(
        f"/api/repos/{OTHER_REPO_ID}/jobs", headers=headers(OTHER_REPO_ID)
    )

    assert mine.status_code == 404
    assert theirs.status_code == 200


# --------------------------------------------------------------------------- #
# The header itself
# --------------------------------------------------------------------------- #


async def test_a_missing_header_is_a_400_not_an_unfiltered_read(client, db_session):
    """The BFF always knows the set, so absence is a caller that forgot.

    This is the assertion that matters most in the file: the failure mode of a caller
    that does not send the header must never be "here is every repo".
    """
    await seed_two_repos(db_session)

    response = await client.get("/api/repos", headers=auth())

    assert response.status_code == 400
    assert AUTHORIZED_REPOS_HEADER in response.json()["detail"]


async def test_a_malformed_header_is_a_400(client, db_session):
    await seed_two_repos(db_session)

    response = await client.get(
        "/api/repos", headers={**auth(), AUTHORIZED_REPOS_HEADER: "12,not-a-number"}
    )

    assert response.status_code == 400


async def test_the_bearer_token_is_still_required_alongside_the_header(client, db_session):
    """The header is trusted *because* of the token. It is not a second way in."""
    await seed_two_repos(db_session)

    response = await client.get(
        "/api/repos", headers={AUTHORIZED_REPOS_HEADER: str(REPO_ID)}
    )

    assert response.status_code == 401


async def test_the_public_board_is_exempt_from_the_header(client, db_session):
    """`/public/flaky` filters `private = false` instead, and takes no header at all."""
    await seed_two_repos(db_session)

    response = await client.get("/public/flaky", params={"window_days": 90})

    assert response.status_code == 200
    # `khoi/flakehound` is public in the fixtures and `khoi/other` is private, so the
    # board answers with real rows and no authorized set — while still excluding the
    # private repo that the authorized reads above needed a header to reach.
    assert {row["repo_full_name"] for row in response.json()} == {"khoi/flakehound"}
