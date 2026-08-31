"""create all SPEC 4 entities

Revision ID: 037a5d2c5519
Revises: 
Create Date: 2026-08-31 10:29:30.852124

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '037a5d2c5519'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('installations',
    sa.Column('id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=True),
    sa.Column('account_login', sa.Text(), nullable=True),
    sa.Column('account_type', sa.Text(), nullable=True),
    sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("account_type IS NULL OR account_type IN ('User', 'Organization')", name=op.f('ck_installations_account_type')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_installations'))
    )
    op.create_table('metrics_snapshots',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('value', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('labels', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_metrics_snapshots')),
    sa.UniqueConstraint('captured_at', 'name', 'labels', name='uq_metrics_snapshots_point')
    )
    op.create_index('ix_metrics_snapshots_series', 'metrics_snapshots', ['name', sa.literal_column('captured_at DESC')], unique=False)
    op.create_table('webhook_deliveries',
    sa.Column('delivery_id', sa.Text(), nullable=False),
    sa.Column('event', sa.Text(), nullable=False),
    sa.Column('action', sa.Text(), nullable=True),
    sa.Column('installation_id', sa.BigInteger(), nullable=True),
    sa.Column('repo_id', sa.BigInteger(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('delivery_id', name=op.f('pk_webhook_deliveries'))
    )
    op.create_table('event_queue',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('delivery_id', sa.Text(), nullable=True),
    sa.Column('job_type', sa.Text(), nullable=False),
    sa.Column('event', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('priority', sa.SmallInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('status', sa.Text(), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default=sa.text('5'), nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'processing', 'done', 'failed')", name=op.f('ck_event_queue_status')),
    sa.ForeignKeyConstraint(['delivery_id'], ['webhook_deliveries.delivery_id'], name=op.f('fk_event_queue_delivery_id')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_queue'))
    )
    op.create_index('ix_event_queue_dequeue', 'event_queue', ['priority', 'created_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index('ix_event_queue_stuck', 'event_queue', ['locked_at'], unique=False, postgresql_where=sa.text("status = 'processing'"))
    op.create_table('repositories',
    sa.Column('id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('installation_id', sa.BigInteger(), nullable=False),
    sa.Column('owner', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('full_name', sa.Text(), nullable=False),
    sa.Column('private', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('default_branch', sa.Text(), nullable=True),
    sa.Column('backfill_status', sa.Text(), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('backfill_window_end', sa.Date(), nullable=True),
    sa.Column('backfill_window_start', sa.Date(), nullable=True),
    sa.Column('backfill_page', sa.Integer(), nullable=True),
    sa.Column('backfill_completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("backfill_status IN ('pending', 'running', 'done', 'failed')", name=op.f('ck_repositories_backfill_status')),
    sa.ForeignKeyConstraint(['installation_id'], ['installations.id'], name=op.f('fk_repositories_installation_id')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_repositories'))
    )
    op.create_index('ix_repositories_installation_id', 'repositories', ['installation_id'], unique=False)
    op.create_index('ix_repositories_public', 'repositories', ['id'], unique=False, postgresql_where=sa.text('private = false'))
    op.create_table('flake_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('repo_id', sa.BigInteger(), nullable=False),
    sa.Column('signal', sa.Text(), nullable=False),
    sa.Column('workflow_id', sa.BigInteger(), nullable=True),
    sa.Column('job_name', sa.Text(), nullable=False),
    sa.Column('head_sha', sa.Text(), nullable=True),
    sa.Column('run_id', sa.BigInteger(), nullable=True),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("signal IN ('rerun_recovery', 'same_commit_disagreement')", name=op.f('ck_flake_events_signal')),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], name=op.f('fk_flake_events_repo_id')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_flake_events')),
    sa.UniqueConstraint('repo_id', 'signal', 'workflow_id', 'job_name', 'head_sha', 'run_id', name='uq_flake_events_group', postgresql_nulls_not_distinct=True)
    )
    op.create_index('ix_flake_events_recent', 'flake_events', ['repo_id', sa.literal_column('occurred_at DESC')], unique=False)
    op.create_table('job_stats_daily',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('repo_id', sa.BigInteger(), nullable=False),
    sa.Column('workflow_id', sa.BigInteger(), nullable=True),
    sa.Column('job_name', sa.Text(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('runs', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('failures', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('opportunities', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('flakes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('duration_p50_seconds', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('duration_p95_seconds', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('duration_total_seconds', sa.Numeric(precision=14, scale=3), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], name=op.f('fk_job_stats_daily_repo_id')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_stats_daily')),
    sa.UniqueConstraint('repo_id', 'workflow_id', 'job_name', 'day', name='uq_job_stats_daily_key', postgresql_nulls_not_distinct=True)
    )
    op.create_index('ix_job_stats_daily_window', 'job_stats_daily', ['repo_id', sa.literal_column('day DESC')], unique=False)
    op.create_table('workflows',
    sa.Column('id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('repo_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.Text(), nullable=True),
    sa.Column('path', sa.Text(), nullable=True),
    sa.Column('state', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], name=op.f('fk_workflows_repo_id')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workflows'))
    )
    op.create_index('ix_workflows_repo_id', 'workflows', ['repo_id'], unique=False)
    op.create_table('workflow_runs',
    sa.Column('run_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('run_attempt', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('repo_id', sa.BigInteger(), nullable=False),
    sa.Column('workflow_id', sa.BigInteger(), nullable=True),
    sa.Column('head_sha', sa.Text(), nullable=False),
    sa.Column('head_branch', sa.Text(), nullable=True),
    sa.Column('event', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), nullable=True),
    sa.Column('conclusion', sa.Text(), nullable=True),
    sa.Column('run_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('github_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('github_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], name=op.f('fk_workflow_runs_repo_id')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_workflow_runs_workflow_id')),
    sa.PrimaryKeyConstraint('run_id', 'run_attempt', name=op.f('pk_workflow_runs'))
    )
    op.create_index('ix_workflow_runs_timeline', 'workflow_runs', ['repo_id', sa.literal_column('run_started_at DESC')], unique=False)
    op.create_table('jobs',
    sa.Column('id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('run_attempt', sa.Integer(), nullable=False),
    sa.Column('repo_id', sa.BigInteger(), nullable=False),
    sa.Column('workflow_id', sa.BigInteger(), nullable=True),
    sa.Column('head_sha', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=True),
    sa.Column('conclusion', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('runner_name', sa.Text(), nullable=True),
    sa.Column('runner_labels', sa.ARRAY(sa.Text()), nullable=True),
    sa.Column('step_count', sa.Integer(), nullable=True),
    sa.Column('completed_step_count', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], name=op.f('fk_jobs_repo_id')),
    sa.ForeignKeyConstraint(['run_id', 'run_attempt'], ['workflow_runs.run_id', 'workflow_runs.run_attempt'], name='fk_jobs_run'),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_jobs_workflow_id')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs'))
    )
    op.create_index('ix_jobs_signal_a', 'jobs', ['repo_id', 'run_id', 'name', 'run_attempt'], unique=False)
    op.create_index('ix_jobs_signal_b', 'jobs', ['repo_id', 'workflow_id', 'name', 'head_sha'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_jobs_signal_b', table_name='jobs')
    op.drop_index('ix_jobs_signal_a', table_name='jobs')
    op.drop_table('jobs')
    op.drop_index('ix_workflow_runs_timeline', table_name='workflow_runs')
    op.drop_table('workflow_runs')
    op.drop_index('ix_workflows_repo_id', table_name='workflows')
    op.drop_table('workflows')
    op.drop_index('ix_job_stats_daily_window', table_name='job_stats_daily')
    op.drop_table('job_stats_daily')
    op.drop_index('ix_flake_events_recent', table_name='flake_events')
    op.drop_table('flake_events')
    op.drop_index('ix_repositories_public', table_name='repositories', postgresql_where=sa.text('private = false'))
    op.drop_index('ix_repositories_installation_id', table_name='repositories')
    op.drop_table('repositories')
    op.drop_index('ix_event_queue_stuck', table_name='event_queue', postgresql_where=sa.text("status = 'processing'"))
    op.drop_index('ix_event_queue_dequeue', table_name='event_queue', postgresql_where=sa.text("status = 'pending'"))
    op.drop_table('event_queue')
    op.drop_table('webhook_deliveries')
    op.drop_index('ix_metrics_snapshots_series', table_name='metrics_snapshots')
    op.drop_table('metrics_snapshots')
    op.drop_table('installations')
