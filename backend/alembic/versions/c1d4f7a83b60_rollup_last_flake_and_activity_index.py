"""job_stats_daily.last_flake_at, and an index on recent job activity

Revision ID: c1d4f7a83b60
Revises: 9f1c4b7ad2e0
Create Date: 2026-09-02 08:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c1d4f7a83b60'
down_revision: str | None = '9f1c4b7ad2e0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'job_stats_daily',
        sa.Column('last_flake_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_jobs_recent_activity', 'jobs', ['updated_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_jobs_recent_activity', table_name='jobs')
    op.drop_column('job_stats_daily', 'last_flake_at')
