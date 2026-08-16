"""session PK → (credential_id, target_id) — one storage_state per persona × platform (design §3.5).

The multi-platform keying fix (design §2 keying bug / §3.5 / D14.1): the session PK was `credential_id`
alone, so one persona logging into two platforms clobbered a single row. The new composite PK
`(credential_id, target_id)` gives one storage_state per persona × platform.

`schema.py`'s DDL (executed by 0001) is left at the original single-column PK on purpose, so this ALTER
applies identically on a fresh DB (0001 old-shape → this composite) and on the persistent gate DB
(0001 already applied → this alters). Existing session rows are an ephemeral storage_state CACHE with
no clean backfill for the new target dimension (v1 has no production data) — they are cleared and
re-seeded lazily on the next run's probe gate.

Chained AFTER main's 0003–0006 (Wave A/B) at integration time — this revision was authored as 0003 on
the auth branch but re-chained to 0007 to resolve the revision-id collision with 0003_incident_member.

Revision ID: 0007_session_per_target
Revises: 0006_test_golden
Create Date: 2026-07-16
"""
from alembic import op

revision = "0007_session_per_target"
down_revision = "0006_test_golden"
branch_labels = None
depends_on = None

_UP = r"""
alter table session drop constraint session_pkey;
delete from session;                                         -- ephemeral cache; no target backfill
alter table session add column target_id uuid not null references target(id);
alter table session add primary key (credential_id, target_id);
"""

_DOWN = r"""
alter table session drop constraint session_pkey;
delete from session;
alter table session drop column target_id;
alter table session add primary key (credential_id);
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
