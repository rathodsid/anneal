"""incident_member.ruling — the per-member decision (A5, money-shot Direction B)

The chosen inbox UX rules each affected test individually (✓ feature / ✕ bug) in one atomic POST;
the incident-level ruling becomes derived (feature|bug|mixed). This column records what was decided
per member — the evidence trail behind a 'mixed' incident.

Revision ID: 0003_incident_member_ruling
Revises: 0002_incident_rep_concluded
Create Date: 2026-07-16
"""
from alembic import op

revision = "0003_incident_member_ruling"
down_revision = "0002_incident_rep_concluded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table incident_member add column if not exists ruling text")


def downgrade() -> None:
    op.execute("alter table incident_member drop column if exists ruling")
