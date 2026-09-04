"""A repository is either installed or publicly observed

Makes SPEC §4's amended repository entity real: `installation_id` becomes nullable,
`source` says which kind of repo a row is, and a check constraint pairs the two so an
observed repo is structurally forbidden from being private.

Safe on a populated table. Every existing row is an installed repo with a non-null
installation, which is exactly what the server default and the constraint assert, so
the constraint validates without a rewrite and nothing needs backfilling.

Revision ID: e4c9a1b73d52
Revises: b8e5309fa14c
Create Date: 2026-09-03 12:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e4c9a1b73d52'
down_revision: str | None = 'b8e5309fa14c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'repositories',
        sa.Column(
            'source',
            sa.Text(),
            nullable=False,
            server_default=sa.text("'installed'"),
        ),
    )
    op.alter_column(
        'repositories',
        'installation_id',
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.create_check_constraint(
        op.f('ck_repositories_source'),
        'repositories',
        "source IN ('installed', 'observed')",
    )
    op.create_check_constraint(
        op.f('ck_repositories_source_installation'),
        'repositories',
        "(source = 'installed' AND installation_id IS NOT NULL)"
        " OR (source = 'observed' AND installation_id IS NULL AND private = false)",
    )


def downgrade() -> None:
    # An observed repo cannot be represented once the column is gone, and attaching it
    # to some arbitrary installation would be a fabrication. Drop those rows instead.
    # They are re-crawlable from public GitHub, which is the whole point of them.
    #
    # Their facts have to go first: five tables reference `repositories.id` and none of
    # those foreign keys cascade, so deleting the repo alone raises. Children before
    # parents, and `jobs` before `workflow_runs` because of the composite run key.
    observed = "SELECT id FROM repositories WHERE source = 'observed'"
    for table in (
        'flake_events',
        'job_stats_daily',
        'jobs',
        'workflow_runs',
        'workflows',
    ):
        op.execute(f"DELETE FROM {table} WHERE repo_id IN ({observed})")
    op.execute("DELETE FROM repositories WHERE source = 'observed'")
    op.drop_constraint(
        op.f('ck_repositories_source_installation'), 'repositories', type_='check'
    )
    op.drop_constraint(op.f('ck_repositories_source'), 'repositories', type_='check')
    op.alter_column(
        'repositories',
        'installation_id',
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.drop_column('repositories', 'source')
