"""suite_run.auth_reason + needs_human — expose the verbatim soft-relogin reason + hard-auth flag.

The probe gate (design §3.5) already computes a verbatim `auth_reason` (e.g. "no credentials for
<ref>") and `needs_human` (captcha/OTP/SSO) on the in-memory SuiteRunReport, but only the machine code
`park_reason='AUTH_EXPIRED'` was persisted. The auth UI needs the human reason + the needs-human badge,
so persist both on suite_run and expose them on SuiteRunDetail (AUTH-FRONTEND-SPEC §6 gap #1).

As with 0007, schema.py's DDL (0001's payload) is left unchanged so this ALTER applies identically on a
fresh DB (0001 without the columns → this adds them) and on the persistent gate DB.

Revision ID: 0008_suite_run_auth_fields
Revises: 0007_session_per_target
Create Date: 2026-07-16
"""
from alembic import op

revision = "0008_suite_run_auth_fields"
down_revision = "0007_session_per_target"
branch_labels = None
depends_on = None

_UP = r"""
alter table suite_run add column auth_reason text;
alter table suite_run add column needs_human boolean not null default false;
"""

_DOWN = r"""
alter table suite_run drop column needs_human;
alter table suite_run drop column auth_reason;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
