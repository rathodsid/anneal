"""incident.rep_concluded — event-driven guided-member wait signal (§9.4)

The discoverer ("representative") re-authors blind; guided members wait for its change_brief. This flag,
set on EVERY discoverer exit path (success/failure/exhaust/crash), lets a waiter break the moment the
discoverer concludes — so it never stalls on a brief that will never come, and never gives up early on a
brief that is still coming (the fixed-timeout failure mode).

Revision ID: 0002_incident_rep_concluded
Revises: 0001_initial
Create Date: 2026-07-12
"""
from alembic import op

revision = "0002_incident_rep_concluded"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table incident add column if not exists "
               "rep_concluded boolean not null default false")


def downgrade() -> None:
    op.execute("alter table incident drop column if exists rep_concluded")
