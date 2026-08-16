"""initial schema — the §16 entities + all four concurrency primitives (LLD §3)

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-09
"""
from alembic import op

from anneal_db.schema import DDL, DROP

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute(DROP)
