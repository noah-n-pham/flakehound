"""The data-model invariants, asserted against real Postgres."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EventQueue,
    FlakeEvent,
    Installation,
    Job,
    JobStatsDaily,
    Repository,
    WebhookDelivery,
    WorkflowRun,
)

REPO_ID = 900_001
INSTALLATION_ID = 800_001
SHA = "a" * 40


async def seed_repo(session: AsyncSession) -> Repository:
    session.add(Installation(id=INSTALLATION_ID, account_login="khoi", account_type="User"))
    repo = Repository(
        id=REPO_ID,
        installation_id=INSTALLATION_ID,
        owner="khoi",
        name="flakehound",
        full_name="khoi/flakehound",
        private=False,
    )
    session.add(repo)
    await session.flush()
    return repo


OBSERVED_REPO_ID = 900_002


def observed_repo(*, private: bool = False, installation_id: int | None = None) -> Repository:
    """A repo crawled from public GitHub. No installation, and it must be public."""
    return Repository(
        id=OBSERVED_REPO_ID,
        installation_id=installation_id,
        source="observed",
        owner="astral-sh",
        name="uv",
        full_name="astral-sh/uv",
        private=private,
    )


def make_run(run_id: int, attempt: int, conclusion: str | None = None) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        run_attempt=attempt,
        repo_id=REPO_ID,
        head_sha=SHA,
        conclusion=conclusion,
        run_started_at=datetime.now(UTC),
    )


async def test_a_rerun_is_a_second_attempt_under_the_same_run_id(db_session):
    await seed_repo(db_session)
    db_session.add_all([make_run(1, 1, "failure"), make_run(1, 2, "success")])
    await db_session.flush()

    attempts = (
        await db_session.execute(
            select(WorkflowRun.run_attempt, WorkflowRun.conclusion)
            .where(WorkflowRun.run_id == 1)
            .order_by(WorkflowRun.run_attempt)
        )
    ).all()
    assert attempts == [(1, "failure"), (2, "success")]


async def test_the_same_run_attempt_cannot_be_stored_twice(db_session):
    await seed_repo(db_session)
    db_session.add(make_run(2, 1))
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(make_run(2, 1))
            await db_session.flush()


async def test_a_duplicate_delivery_is_rejected_by_the_primary_key(db_session):
    """Idempotency layer 1: the constraint *is* the dedup mechanism."""
    delivery_id = "1f2e3d4c-0000-4000-8000-000000000001"
    db_session.add(WebhookDelivery(delivery_id=delivery_id, event="workflow_job"))
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(WebhookDelivery(delivery_id=delivery_id, event="workflow_job"))
            await db_session.flush()


async def test_backfill_work_is_enqueued_without_a_delivery(db_session):
    row = EventQueue(job_type="backfill_runs", payload={"repo_id": REPO_ID}, priority=1)
    db_session.add(row)
    await db_session.flush()

    assert row.delivery_id is None
    assert (row.status, row.attempts, row.priority) == ("pending", 0, 1)


async def test_the_queue_refuses_an_unknown_status(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(EventQueue(job_type="webhook", payload={}, status="in_flight"))
            await db_session.flush()


async def test_job_names_keep_their_matrix_values(db_session):
    await seed_repo(db_session)
    db_session.add(make_run(3, 1))
    await db_session.flush()

    names = ["test (ubuntu-latest, 3.11)", "test (ubuntu-latest, 3.12)"]
    db_session.add_all(
        [
            Job(
                id=5_000 + i,
                run_id=3,
                run_attempt=1,
                repo_id=REPO_ID,
                head_sha=SHA,
                name=name,
                conclusion="success",
            )
            for i, name in enumerate(names)
        ]
    )
    await db_session.flush()

    stored = (
        await db_session.execute(select(Job.name).where(Job.run_id == 3).order_by(Job.name))
    ).scalars()
    assert list(stored) == names


async def test_a_job_cannot_belong_to_a_run_attempt_that_does_not_exist(db_session):
    await seed_repo(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Job(id=6_000, run_id=404, run_attempt=1, repo_id=REPO_ID, head_sha=SHA, name="test")
            )
            await db_session.flush()


async def test_re_evaluating_the_same_history_cannot_duplicate_a_flake_event(db_session):
    """Idempotency layer 3, with the NULL columns of the unused grouping key."""
    await seed_repo(db_session)

    def signal_a() -> FlakeEvent:
        return FlakeEvent(
            repo_id=REPO_ID,
            signal="rerun_recovery",
            job_name="test (ubuntu-latest, 3.11)",
            run_id=7,
            evidence={"jobs": [1, 2], "conclusions": ["failure", "success"]},
        )

    db_session.add(signal_a())
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(signal_a())
            await db_session.flush()


async def test_the_two_signals_can_both_fire_for_one_job(db_session):
    await seed_repo(db_session)
    db_session.add_all(
        [
            FlakeEvent(repo_id=REPO_ID, signal="rerun_recovery", job_name="test", evidence={}),
            FlakeEvent(
                repo_id=REPO_ID, signal="same_commit_disagreement", job_name="test", evidence={}
            ),
        ]
    )
    await db_session.flush()

    count = (
        await db_session.execute(
            select(func.count()).select_from(FlakeEvent).where(FlakeEvent.repo_id == REPO_ID)
        )
    ).scalar_one()
    assert count == 2


async def test_a_rollup_row_is_unique_per_repo_workflow_job_and_day(db_session):
    await seed_repo(db_session)

    def stats() -> JobStatsDaily:
        return JobStatsDaily(repo_id=REPO_ID, job_name="test", day=date(2026, 8, 31), runs=1)

    db_session.add(stats())
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(stats())
            await db_session.flush()


# --------------------------------------------------------------------------- #
# Installed or observed — SPEC §4's two kinds of repository
#
# The pairing of `source` with `installation_id` and `private` is the public board's
# privacy guarantee, and the point of putting it in the database is that it holds
# against a write path that forgets. So these tests attack the constraint directly
# rather than going through an upsert.
# --------------------------------------------------------------------------- #


async def test_an_observed_repo_needs_no_installation(db_session):
    """The row the old NOT NULL made impossible, and the reason for this migration."""
    db_session.add(observed_repo())
    await db_session.flush()

    stored = await db_session.get(Repository, OBSERVED_REPO_ID)
    assert (stored.source, stored.installation_id, stored.private) == ("observed", None, False)


async def test_an_observed_repo_cannot_be_private(db_session):
    """**The guarantee.** No care on the write path is required for this to hold."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(observed_repo(private=True))
            await db_session.flush()


async def test_an_observed_repo_cannot_carry_an_installation(db_session):
    """The other half of the pairing: `source` cannot disagree with the foreign key."""
    await seed_repo(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(observed_repo(installation_id=INSTALLATION_ID))
            await db_session.flush()


async def test_an_installed_repo_still_requires_an_installation(db_session):
    """Nullability was relaxed for observed repos only. An installed repo is unchanged."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Repository(
                    id=OBSERVED_REPO_ID,
                    installation_id=None,
                    owner="khoi",
                    name="thing",
                    full_name="khoi/thing",
                    private=False,
                )
            )
            await db_session.flush()


async def test_a_repo_is_installed_unless_it_says_otherwise(db_session):
    """The server default, which is what carried every pre-existing row through."""
    repo = await seed_repo(db_session)

    assert repo.source == "installed"


async def test_source_refuses_a_third_kind_of_repo(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            repo = observed_repo()
            repo.source = "vendored"
            db_session.add(repo)
            await db_session.flush()


async def test_an_observed_repo_that_installs_the_app_upgrades_in_place(db_session):
    """SPEC §4: "becomes installed in place, under the same GitHub repo id".

    This is why `source` is a column rather than a second table. The upgrade keeps the
    row, so it keeps its crawled history, its backfill cursor, and every fact and flake
    event pointing at it — no duplicate repo, no re-crawl, and no identity change.
    """
    db_session.add(Installation(id=INSTALLATION_ID, account_login="astral-sh"))
    db_session.add(observed_repo())
    await db_session.flush()
    db_session.add(
        WorkflowRun(
            run_id=11,
            run_attempt=1,
            repo_id=OBSERVED_REPO_ID,
            head_sha=SHA,
            conclusion="failure",
        )
    )
    await db_session.flush()

    upgraded = await db_session.get(Repository, OBSERVED_REPO_ID)
    upgraded.source = "installed"
    upgraded.installation_id = INSTALLATION_ID
    await db_session.flush()

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(Repository)
            .where(Repository.full_name == "astral-sh/uv")
        )
    ).scalar_one()
    assert count == 1
    # The run was crawled against the observed repo and still points at the same id.
    runs = (
        await db_session.execute(
            select(WorkflowRun.run_id).where(WorkflowRun.repo_id == OBSERVED_REPO_ID)
        )
    ).scalars()
    assert list(runs) == [11]
