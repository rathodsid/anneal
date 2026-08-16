"""test_golden — the per-test golden reference recording (B4, design/BACKEND-SPEC.md)

The human source-of-truth "how this test should look", complementing the machine oracle (test-viewer
golden panel). One per test: auto-seeded at freeze from a clean cold replay of the frozen bundle
(source='auto'), a reviewer upload overrides it (source='manual'). Bytes live in the artifact store;
this row is the pointer + provenance.

Revision ID: 0006_test_golden
Revises: 0005_run_role_verify_link
Create Date: 2026-07-16
"""
from alembic import op

revision = "0006_test_golden"
down_revision = "0005_run_role_verify_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table if not exists test_golden (
          test_id          uuid primary key references test(id),
          video_path       text not null,
          source           text not null,
          reflects_version int,
          created_at       timestamptz not null default now()
        )""")


def downgrade() -> None:
    op.execute("drop table if exists test_golden")
