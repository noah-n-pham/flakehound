"""The `installation` and `installation_repositories` handlers (SPEC §7).

The rule these tests exist to pin: an install being removed changes what we
*watch*, never what we *recorded*. Nothing here deletes a fact.
"""

import pytest
from sqlalchemy import func, select

from app.models import EventQueue, Installation, Job, Repository, WorkflowRun
from app.worker import run_once
from tests import payloads
from tests.helpers import deliver, enqueue
from tests.helpers import one_session_factory as _one_session_factory


async def _installation(session) -> Installation:
    session.expire_all()
    return (await session.execute(select(Installation))).scalar_one()


async def _repo(session, repo_id: int = payloads.REPO_ID) -> Repository:
    session.expire_all()
    return (
        await session.execute(select(Repository).where(Repository.id == repo_id))
    ).scalar_one()


async def test_a_new_install_records_the_account_and_its_repositories(db_session):
    await deliver(db_session, payloads.installation_event(action="created"))

    installation = await _installation(db_session)
    assert installation.id == payloads.INSTALLATION_ID
    assert (installation.account_login, installation.account_type) == ("khoi", "User")
    assert installation.suspended_at is None
    assert installation.deleted_at is None

    repo = await _repo(db_session)
    assert repo.full_name == "khoi/flakehound"
    assert repo.active is True
    # A minimal repository carries no owner object, so the owner comes from full_name.
    assert repo.owner == "khoi"
    assert repo.private is False


async def test_an_install_fills_in_a_row_a_job_event_stubbed(db_session):
    """The account fields arrive only here, and a job event usually arrives first."""
    await deliver(db_session, payloads.workflow_job())
    stub = await _installation(db_session)
    assert stub.account_login == "khoi"
    assert stub.account_id == 41_000_000

    await deliver(
        db_session,
        payloads.installation_event(
            action="created", account_login="acme", account_type="Organization"
        ),
    )

    filled = await _installation(db_session)
    assert (filled.account_login, filled.account_type) == ("acme", "Organization")
    installs = await db_session.execute(select(func.count()).select_from(Installation))
    assert installs.scalar_one() == 1


async def test_suspending_and_unsuspending_an_install(db_session):
    await deliver(db_session, payloads.installation_event(action="created"))

    await deliver(
        db_session,
        payloads.installation_event(action="suspend", suspended_at="2026-09-01T12:00:00Z"),
    )
    suspended = await _installation(db_session)
    assert suspended.suspended_at is not None
    assert suspended.suspended_at.isoformat().startswith("2026-09-01T12:00:00")
    # Suspended is not uninstalled: the repos are still ours to watch.
    assert (await _repo(db_session)).active is True

    await deliver(db_session, payloads.installation_event(action="unsuspend"))
    assert (await _installation(db_session)).suspended_at is None


async def test_an_uninstall_deactivates_the_repos_but_keeps_every_fact(db_session):
    await deliver(
        db_session,
        payloads.installation_event(action="created"),
        payloads.workflow_job(),
    )
    assert (await db_session.execute(select(func.count()).select_from(Job))).scalar_one() == 1

    await deliver(db_session, payloads.installation_event(action="deleted"))

    installation = await _installation(db_session)
    assert installation.deleted_at is not None
    assert (await _repo(db_session)).active is False
    # The point of the whole test: history is not ours to throw away.
    assert (await db_session.execute(select(func.count()).select_from(Job))).scalar_one() == 1
    assert (
        await db_session.execute(select(func.count()).select_from(WorkflowRun))
    ).scalar_one() == 1


async def test_reinstalling_revives_the_install_and_its_repos(db_session):
    await deliver(
        db_session,
        payloads.installation_event(action="created"),
        payloads.installation_event(action="deleted"),
        payloads.installation_event(action="created"),
    )

    installation = await _installation(db_session)
    assert installation.deleted_at is None
    assert (await _repo(db_session)).active is True


async def test_repositories_added_to_an_existing_install(db_session):
    await deliver(db_session, payloads.installation_event(action="created"))

    added = payloads.minimal_repository(repo_id=62_000_099, name="other")
    await deliver(
        db_session,
        payloads.installation_repositories_event(action="added", added=[added]),
    )

    repos = (
        await db_session.execute(select(Repository.id, Repository.active).order_by(Repository.id))
    ).all()
    assert repos == [(payloads.REPO_ID, True), (62_000_099, True)]


async def test_a_minimal_repository_leaves_the_branch_unknown_rather_than_guessing(db_session):
    """Pins the shape of a real `repositories_added` entry (turn 26).

    An installation event names a repo with five keys and no owner object, so
    `owner` is derived from `full_name` and `default_branch` stays NULL until an
    event that actually carries it arrives. Defaulting it to `main` would be a
    guess stored as a fact.
    """
    added = payloads.minimal_repository(repo_id=62_000_099, name="other")
    assert set(added) == {"id", "name", "node_id", "full_name", "private"}

    await deliver(
        db_session,
        payloads.installation_repositories_event(action="added", added=[added]),
    )

    repo = await _repo(db_session, 62_000_099)
    assert (repo.owner, repo.name) == ("khoi", "other")
    assert repo.default_branch is None
    assert repo.private is False


async def test_repositories_removed_are_deactivated_not_deleted(db_session):
    await deliver(
        db_session,
        payloads.installation_event(action="created"),
        payloads.workflow_job(),
    )

    await deliver(
        db_session,
        payloads.installation_repositories_event(
            action="removed", removed=[payloads.minimal_repository()]
        ),
    )

    repo = await _repo(db_session)
    assert repo.active is False
    assert repo.full_name == "khoi/flakehound"
    assert (await db_session.execute(select(func.count()).select_from(Job))).scalar_one() == 1


async def test_a_late_job_event_does_not_revive_a_removed_repo(db_session):
    """Only the installation events decide what is installed.

    Deliveries for a removed repo can still be in flight, and treating one as
    proof of installation would flip the flag back on by accident.
    """
    await deliver(
        db_session,
        payloads.installation_event(action="created"),
        payloads.installation_repositories_event(
            action="removed", removed=[payloads.minimal_repository()]
        ),
        payloads.workflow_job(job_id=95_000_099),
    )

    assert (await _repo(db_session)).active is False
    # The event was still processed — the facts are recorded, the repo is just
    # no longer watched.
    assert (await db_session.execute(select(func.count()).select_from(Job))).scalar_one() == 1


async def test_an_installation_repositories_event_can_arrive_first(db_session):
    """The queue accepts any order, so this handler must stub the install too."""
    await deliver(
        db_session,
        payloads.installation_repositories_event(
            action="added", added=[payloads.minimal_repository()]
        ),
    )

    assert (await _installation(db_session)).id == payloads.INSTALLATION_ID
    assert (await _repo(db_session)).active is True


async def test_replaying_the_installation_events_changes_nothing(db_session):
    """Idempotency layer 2 over the control plane."""
    other = payloads.minimal_repository(repo_id=62_000_099, name="other")
    events = (
        payloads.installation_event(action="created"),
        payloads.installation_repositories_event(action="added", added=[other]),
        payloads.installation_repositories_event(action="removed", removed=[other]),
    )
    await deliver(db_session, *events)

    def snapshot(rows):
        return [tuple(row) for row in rows]

    async def read():
        db_session.expire_all()
        installs = snapshot(
            (
                await db_session.execute(
                    select(Installation.id, Installation.account_login, Installation.deleted_at)
                )
            ).all()
        )
        repos = snapshot(
            (
                await db_session.execute(
                    select(Repository.id, Repository.full_name, Repository.active).order_by(
                        Repository.id
                    )
                )
            ).all()
        )
        return installs, repos

    # Spelled out rather than just captured. "Unchanged" is true of an empty
    # database too, so the state being compared has to be asserted to exist.
    before = await read()
    assert before == (
        [(payloads.INSTALLATION_ID, "khoi", None)],
        [
            (payloads.REPO_ID, "khoi/flakehound", True),
            (62_000_099, "khoi/other", False),
        ],
    )

    await deliver(db_session, *events)
    await deliver(db_session, *events)
    assert await read() == before


@pytest.mark.parametrize("event", ["installation", "installation_repositories"])
async def test_a_payload_with_no_installation_is_an_error(db_session, event):
    """It raises, so the queue records the error and retries rather than silently passing."""
    enqueue(db_session, {"action": "created"}, event=event)
    await db_session.flush()

    await run_once(_one_session_factory(db_session), batch_size=10)

    row = (await db_session.execute(select(EventQueue))).scalar_one()
    await db_session.refresh(row)
    assert row.status == "pending"
    assert "ValueError" in row.last_error
