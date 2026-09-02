"""`/public/flaky` — the only read endpoint with no auth (SPEC §8, §8b).

Every test here is really one question: can anything a stranger says, or anything the
build forgets, put a private repo's data on this board. So the filter is attacked from
three directions — a private repo sitting beside a public one, a public repo flipped
private after its rows were already rolled up, and a query parameter smuggled in that
names the private repo by id.
"""

from typing import Any

from sqlalchemy import update

from app.models import Repository
from app.rollup import rollup_repository
from app.stats import public_flaky_jobs
from tests.helpers import deliver
from tests.payloads import REPO_ID
from tests.test_api import reader
from tests.test_detection import OTHER_RUN_ID, OTHER_WORKFLOW_ID, RUN_ID, attempt, run_event

# The default fixture repo (`khoi/flakehound`, public) plus two more, each with its own
# run ids so the synthetic job ids stay distinct — a job id is a primary key, and two
# repos delivering the same attempt would overwrite one row with the other.
SECOND_PUBLIC_REPO_ID = REPO_ID + 1
PRIVATE_REPO_ID = REPO_ID + 2
SECOND_PUBLIC_RUN_ID = OTHER_RUN_ID
PRIVATE_RUN_ID = OTHER_RUN_ID + 1


def in_repo(payload: dict[str, Any], *, repo_id: int, name: str, private: bool) -> dict[str, Any]:
    """The same delivery, sent from a different repository.

    A faithful edit rather than a fixture trick: every real payload carries its whole
    `repository` block, `private` flag included, on every delivery.
    """
    return {
        **payload,
        "repository": {
            **payload["repository"],
            "id": repo_id,
            "name": name,
            "full_name": f"khoi/{name}",
            "private": private,
        },
    }


async def seed_three_repos(session) -> None:
    """Two public repos and a private one, each with a job that really flaked.

    The public repos are given deliberately different amounts of evidence for the same
    behaviour — three attempts against two — because that is what the ranking is
    supposed to separate. They also each get a clean job with the same two runs, which
    ties them exactly and is what the tiebreak has to order.
    """
    await deliver(
        session,
        run_event(run_id=RUN_ID),
        attempt(1, "failure"),
        attempt(2, "failure"),
        attempt(3, "success"),
        attempt(1, "success", name="stable leg"),
        attempt(2, "success", name="stable leg"),
    )
    for repo_id, name, private, run_id in (
        (SECOND_PUBLIC_REPO_ID, "form-check", False, SECOND_PUBLIC_RUN_ID),
        (PRIVATE_REPO_ID, "private-thing", True, PRIVATE_RUN_ID),
    ):
        await deliver(
            session,
            *(
                in_repo(payload, repo_id=repo_id, name=name, private=private)
                for payload in (
                    run_event(run_id=run_id, workflow_id=OTHER_WORKFLOW_ID),
                    attempt(1, "failure", run_id=run_id),
                    attempt(2, "success", run_id=run_id),
                    attempt(1, "success", name="stable leg", run_id=run_id),
                    attempt(2, "success", name="stable leg", run_id=run_id),
                )
            ),
        )
    for repo_id in (REPO_ID, SECOND_PUBLIC_REPO_ID, PRIVATE_REPO_ID):
        await rollup_repository(session, repo_id=repo_id)


async def board(client, **params) -> list[dict[str, Any]]:
    response = await client.get("/public/flaky", params={"window_days": 90, **params})
    assert response.status_code == 200
    return response.json()


def named(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(row["repo_full_name"], row["job_name"]) for row in rows]


# --------------------------------------------------------------------------- #
# No auth, and no way to widen it
# --------------------------------------------------------------------------- #


async def test_the_public_board_needs_no_token(client, db_session):
    await seed_three_repos(db_session)

    response = await client.get("/public/flaky")

    assert response.status_code == 200
    assert isinstance(response.json(), list)


async def test_a_private_repo_never_appears_beside_a_public_one(client, db_session):
    await seed_three_repos(db_session)

    rows = await board(client)

    assert named(rows) == [
        ("khoi/flakehound", "build and deploy"),
        ("khoi/form-check", "build and deploy"),
        ("khoi/flakehound", "stable leg"),
        ("khoi/form-check", "stable leg"),
    ]
    assert PRIVATE_REPO_ID not in {row["repo_id"] for row in rows}
    # And the private repo's flakes are real: the endpoint that requires a token has
    # them, so the absence above is the filter working rather than an empty database.
    private = await client.get(
        f"/api/repos/{PRIVATE_REPO_ID}/flaky",
        params={"window_days": 90},
        headers=reader(PRIVATE_REPO_ID),
    )
    assert [row["job_name"] for row in private.json()] == ["build and deploy", "stable leg"]
    assert private.json()[0]["flakes"] == 2


async def test_a_repo_flipped_to_private_drops_off_the_board(client, db_session):
    """Same rows, same rollup, one flag — and the repo has to vanish.

    Nothing recomputes `job_stats_daily` when a repo goes private, so if the filter
    lived anywhere but the read query, the old rows would keep being served.
    """
    await seed_three_repos(db_session)
    assert ("khoi/flakehound", "build and deploy") in named(await board(client))

    await db_session.execute(
        update(Repository).where(Repository.id == REPO_ID).values(private=True)
    )
    await db_session.flush()

    assert named(await board(client)) == [
        ("khoi/form-check", "build and deploy"),
        ("khoi/form-check", "stable leg"),
    ]


async def test_an_uninstalled_repo_drops_off_the_board(client, db_session):
    """D-042: removing the App is the nearest thing to withdrawing consent."""
    await seed_three_repos(db_session)

    await db_session.execute(
        update(Repository).where(Repository.id == REPO_ID).values(active=False)
    )
    await db_session.flush()

    assert REPO_ID not in {row["repo_id"] for row in await board(client)}


async def test_a_repo_id_in_the_query_string_cannot_widen_the_board(client, db_session):
    """The endpoint takes no repo id at all, which is why there is nothing to distrust."""
    await seed_three_repos(db_session)

    smuggled = await board(client, repo_id=PRIVATE_REPO_ID)

    assert named(smuggled) == named(await board(client))


# --------------------------------------------------------------------------- #
# What the rows say
# --------------------------------------------------------------------------- #


async def test_a_row_carries_the_counts_and_the_whole_interval(client, db_session):
    await seed_three_repos(db_session)

    row = (await board(client))[0]

    assert row["repo_id"] == REPO_ID
    assert (row["opportunities"], row["failures"], row["flakes"]) == (3, 2, 3)
    assert row["flake_rate"] == 1.0
    assert row["wilson_upper"] == 1.0
    assert 0 < row["wilson_lower"] < 1
    assert row["last_flake_at"] is not None


async def test_the_board_ranks_across_repos_by_the_wilson_lower_bound(client, db_session):
    """Three attempts of the same behaviour outrank two, and a clean job ranks last.

    Both flaky jobs have a raw rate of 1.0, so raw rate cannot order them at all —
    what separates them is how much has been seen, which is the whole point of ranking
    on the lower bound.
    """
    await seed_three_repos(db_session)

    rows = await board(client)

    bounds = [row["wilson_lower"] for row in rows]
    assert bounds == sorted(bounds, reverse=True)
    assert [row["flake_rate"] for row in rows[:2]] == [1.0, 1.0]
    assert rows[0]["opportunities"] > rows[1]["opportunities"]
    assert rows[-1]["flakes"] == 0
    assert rows[-1]["wilson_lower"] == 0.0


async def test_rows_tied_on_evidence_are_ordered_deterministically(client, db_session):
    """Both clean jobs are tied exactly — zero flakes in two runs each — and most of a
    real public board looks like that. Without a tiebreak, `limit` would cut off
    whichever of them the aggregate happened to return second."""
    await seed_three_repos(db_session)

    rows = await board(client)
    tied = [row for row in rows if (row["wilson_lower"], row["opportunities"]) == (0.0, 2)]

    assert [row["repo_full_name"] for row in tied] == ["khoi/flakehound", "khoi/form-check"]
    assert named(await board(client, limit=3)) == named(rows)[:3]


async def test_the_board_is_empty_until_the_rollup_has_run(client, db_session):
    """Reads come from the rollup, so a public repo with facts and no rollup is absent."""
    await deliver(
        db_session, run_event(run_id=RUN_ID), attempt(1, "failure"), attempt(2, "success")
    )

    assert await board(client) == []

    await rollup_repository(db_session, repo_id=REPO_ID)

    assert named(await board(client)) == [("khoi/flakehound", "build and deploy")]


async def test_a_public_repo_with_no_opportunities_is_absent_rather_than_undefined(
    client, db_session
):
    """Production's only public repo is exactly this: installed, no CI history."""
    await deliver(
        db_session,
        *(
            in_repo(payload, repo_id=SECOND_PUBLIC_REPO_ID, name="form-check", private=False)
            for payload in (attempt(1, "cancelled", run_id=SECOND_PUBLIC_RUN_ID),)
        ),
    )
    await rollup_repository(db_session, repo_id=SECOND_PUBLIC_REPO_ID)

    assert await board(client) == []


# --------------------------------------------------------------------------- #
# Window and limit
# --------------------------------------------------------------------------- #


async def test_the_board_honours_its_window(client, db_session):
    await seed_three_repos(db_session)

    # The fixtures completed on 2026-08-31, so a one-day window holds nothing.
    assert await board(client, window_days=1) == []


async def test_the_board_honours_its_limit(client, db_session):
    await seed_three_repos(db_session)

    assert len(await board(client, limit=1)) == 1


async def test_the_board_rejects_a_nonsense_window(client, db_session):
    response = await client.get("/public/flaky", params={"window_days": 0})

    assert response.status_code == 422


async def test_the_query_ignores_a_private_repo_even_called_directly(db_session):
    """One level below the route, in case a later endpoint reuses the query."""
    await seed_three_repos(db_session)

    rows = await public_flaky_jobs(db_session, window_days=90)

    assert {row.repo_id for row in rows} == {REPO_ID, SECOND_PUBLIC_REPO_ID}
