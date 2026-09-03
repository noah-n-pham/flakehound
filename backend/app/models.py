"""Every entity in SPEC §4, in one place.

Identity keys are binding and freeze at Checkpoint 1:

* an installation, repository, workflow, job, and delivery are keyed on the id
  GitHub gives them;
* a **workflow run is keyed on (run_id, run_attempt)**, never run id alone;
* dedup is `webhook_deliveries.delivery_id` being the primary key — nothing
  outside Postgres participates.

Columns, types, and indexes are implementation choices and may change.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

QUEUE_STATUSES = ("pending", "processing", "done", "failed")
BACKFILL_STATUSES = ("pending", "running", "done", "failed")
SIGNALS = ("rerun_recovery", "same_commit_disagreement")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def created_at_column() -> Mapped[datetime]:
    """Retained on fact tables so partitioning stays possible later (SPEC §4)."""
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# Control plane
# --------------------------------------------------------------------------- #


class Installation(Base):
    """One per GitHub App installation, keyed on GitHub's installation id.

    Account fields are nullable because a `workflow_job` payload carries only
    `installation.id`; a stub row is upserted then and filled in when the
    `installation` event arrives.
    """

    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    account_login: Mapped[str | None] = mapped_column(Text)
    account_type: Mapped[str | None] = mapped_column(Text)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            "account_type IS NULL OR account_type IN ('User', 'Organization')",
            name="account_type",
        ),
    )


class Repository(Base):
    """One per repo, keyed on GitHub's repo id. Carries the backfill cursor (SPEC §7)."""

    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    installation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("installations.id"), nullable=False
    )
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    default_branch: Mapped[str | None] = mapped_column(Text)

    backfill_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    # The runs listing caps near 1000 results, so backfill walks `created` windows
    # backwards. These three resume it rather than restart it.
    backfill_window_end: Mapped[date | None] = mapped_column(Date)
    backfill_window_start: Mapped[date | None] = mapped_column(Date)
    backfill_page: Mapped[int | None] = mapped_column(Integer)
    backfill_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(_in("backfill_status", BACKFILL_STATUSES), name="backfill_status"),
        Index("ix_repositories_installation_id", "installation_id"),
        # /public/flaky filters on this.
        Index("ix_repositories_public", "id", postgresql_where=text("private = false")),
    )


class Workflow(Base):
    """One per workflow definition, keyed on GitHub's workflow id."""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    repo_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("repositories.id"), nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (Index("ix_workflows_repo_id", "repo_id"),)


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


class WebhookDelivery(Base):
    """Idempotency layer 1. The primary key *is* the dedup mechanism (SPEC §6).

    A duplicate delivery raises a unique violation, which the handler catches and
    answers 202 without enqueueing.
    """

    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str | None] = mapped_column(Text)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    repo_id: Mapped[int | None] = mapped_column(BigInteger)
    # Start of the ingest-lag measurement that ends at event_queue.completed_at.
    received_at: Mapped[datetime] = created_at_column()


class EventQueue(Base):
    """The work list, dequeued with FOR UPDATE SKIP LOCKED (SPEC §5).

    `delivery_id` is nullable on purpose: backfill work is enqueued by the worker
    and has no inbound delivery behind it. Live rows are inserted in the same
    transaction as their delivery row.
    """

    __tablename__ = "event_queue"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    delivery_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("webhook_deliveries.delivery_id")
    )
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # 0 = live webhook, 1 = backfill. Live always wins.
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when an attempt fails, so the retry waits instead of burning the whole
    # attempt ceiling inside one second on a failure that is merely transient.
    # NULL means claimable now, which is what every live row is.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(_in("status", QUEUE_STATUSES), name="status"),
        # Query: claim a batch — pending, attempts left, backoff elapsed, priority
        # then age, FOR UPDATE SKIP LOCKED. The backoff test stays out of the
        # predicate because `now()` is not immutable; it filters the rows this
        # index already narrowed to.
        Index(
            "ix_event_queue_dequeue",
            "priority",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # Query: the reaper's sweep for rows claimed longer than the timeout.
        Index(
            "ix_event_queue_stuck",
            "locked_at",
            postgresql_where=text("status = 'processing'"),
        ),
    )


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #


class WorkflowRun(Base):
    """Identity is (run_id, run_attempt). A re-run is a new attempt, and both are real.

    `workflow_id` is nullable because a `workflow_job` payload does not carry it;
    such a run is stubbed from the job and enriched when its run event arrives.
    """

    __tablename__ = "workflow_runs"

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    run_attempt: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    repo_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("repositories.id"), nullable=False)
    workflow_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("workflows.id"))
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    head_branch: Mapped[str | None] = mapped_column(Text)
    event: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    conclusion: Mapped[str | None] = mapped_column(Text)
    run_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    github_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    github_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        # Query: the dashboard's reverse-chronological run timeline for a repo.
        Index("ix_workflow_runs_timeline", "repo_id", text("run_started_at DESC")),
    )


class Job(Base):
    """One per job execution, keyed on GitHub's job id.

    `name` is stored whole, matrix values included (`test (ubuntu-latest, 3.11)`).
    Different legs are different jobs and are never normalized together.

    `head_sha` and `workflow_id` are denormalized off the run so Signal B's
    grouping query needs no join — the hottest query in the system.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    repo_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("repositories.id"), nullable=False)
    workflow_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("workflows.id"))
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str | None] = mapped_column(Text)
    conclusion: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runner_name: Mapped[str | None] = mapped_column(Text)
    runner_labels: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # Zero completed steps is how a dead runner is told from a test failure
    # (SPEC §2 edge-case table).
    step_count: Mapped[int | None] = mapped_column(Integer)
    completed_step_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "run_attempt"],
            ["workflow_runs.run_id", "workflow_runs.run_attempt"],
            name="fk_jobs_run",
        ),
        # Query: Signal B — conclusions for one (workflow, job name, sha) group.
        Index("ix_jobs_signal_b", "repo_id", "workflow_id", "name", "head_sha"),
        # Query: Signal A — attempts of one job within one run, in attempt order.
        Index("ix_jobs_signal_a", "repo_id", "run_id", "name", "run_attempt"),
        # Query: `/api/repos/{id}/jobs` — one repo's newest executions.
        #
        # The ordering is spelled out because a b-tree only satisfies a sort it
        # matches exactly, and `DESC` alone would put NULLS FIRST. Without this the
        # endpoint sequentially scanned the repo's whole job table to return fifty
        # rows: 45 ms against 60k rows, and linear in history from there.
        Index(
            "ix_jobs_repo_recent",
            "repo_id",
            text("started_at DESC NULLS LAST"),
            text("id DESC"),
        ),
        # Query: which repos the rollup sweep must recompute — the repos whose job
        # rows moved since the last pass.
        Index("ix_jobs_recent_activity", "updated_at"),
    )


# --------------------------------------------------------------------------- #
# Derived
# --------------------------------------------------------------------------- #


class FlakeEvent(Base):
    """Idempotency layer 3: unique on the grouping key plus the signal.

    The grouping key differs per signal — Signal A groups by run, Signal B by
    (workflow, sha) — so the unused columns are NULL and the constraint is
    declared NULLS NOT DISTINCT, which makes re-evaluating the same history a
    no-op instead of a duplicate.
    """

    __tablename__ = "flake_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    repo_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("repositories.id"), nullable=False)
    signal: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_id: Mapped[int | None] = mapped_column(BigInteger)
    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    head_sha: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    # The job ids and conclusions that triggered it.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(_in("signal", SIGNALS), name="signal"),
        UniqueConstraint(
            "repo_id",
            "signal",
            "workflow_id",
            "job_name",
            "head_sha",
            "run_id",
            name="uq_flake_events_group",
            postgresql_nulls_not_distinct=True,
        ),
        # Query: the leaderboard's recent flake events for a repo.
        Index("ix_flake_events_recent", "repo_id", text("occurred_at DESC")),
    )


class JobStatsDaily(Base):
    """The rollup every read endpoint is served from — never raw facts (SPEC §8).

    One row per (repo, workflow, job name, UTC day). `opportunities` and `flakes`
    are SPEC §2's two counts, so a window of these rows sums to the same flake rate
    the raw facts give — see `app/rollup.py` for why each count is what it is.
    """

    __tablename__ = "job_stats_daily"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    repo_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("repositories.id"), nullable=False)
    workflow_id: Mapped[int | None] = mapped_column(BigInteger)
    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)

    runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    opportunities: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    flakes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    duration_p50_seconds: Mapped[float | None] = mapped_column(Numeric(12, 3))
    duration_p95_seconds: Mapped[float | None] = mapped_column(Numeric(12, 3))
    duration_total_seconds: Mapped[float | None] = mapped_column(Numeric(14, 3))
    # The latest implicated job run of this day, so the leaderboard can report when a
    # job last flaked without reading `flake_events` beside the rollup.
    last_flake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint(
            "repo_id",
            "workflow_id",
            "job_name",
            "day",
            name="uq_job_stats_daily_key",
            postgresql_nulls_not_distinct=True,
        ),
        # Query: leaderboard and minutes attribution over a trailing window.
        Index("ix_job_stats_daily_window", "repo_id", text("day DESC")),
    )


class MetricsSnapshot(Base):
    """One row per counter per minute (SPEC §9). Labels carry per-installation series."""

    __tablename__ = "metrics_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint("captured_at", "name", "labels", name="uq_metrics_snapshots_point"),
        # Query: one counter's series over a time range.
        Index("ix_metrics_snapshots_series", "name", text("captured_at DESC")),
    )
