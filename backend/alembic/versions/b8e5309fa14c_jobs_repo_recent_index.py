"""an index for one repo's newest job executions

`/api/repos/{id}/jobs` sequentially scanned the repo's entire job table to return
fifty rows — 45 ms against 60k, and linear in history from there. The ordering is
spelled out because a b-tree only satisfies a sort it matches exactly, and `DESC`
alone would mean NULLS FIRST while the query asks for NULLS LAST.

Revision ID: b8e5309fa14c
Revises: c1d4f7a83b60
Create Date: 2026-09-03 11:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b8e5309fa14c'
down_revision: str | None = 'c1d4f7a83b60'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        'ix_jobs_repo_recent',
        'jobs',
        ['repo_id', sa.text('started_at DESC NULLS LAST'), sa.text('id DESC')],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_jobs_repo_recent', table_name='jobs')
