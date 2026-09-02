"""event_queue.next_attempt_at for retry backoff

Revision ID: 9f1c4b7ad2e0
Revises: 037a5d2c5519
Create Date: 2026-09-01 21:05:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '9f1c4b7ad2e0'
down_revision: str | None = '037a5d2c5519'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'event_queue',
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('event_queue', 'next_attempt_at')
