"""test.labels — free-form operator labels on a test (smoke / sanity / regression / …).

Labels are CATALOG METADATA, not part of the frozen artifact: heal and re-author mint versions
unattended, so a bundle-resident label would need copy-forward in every version writer, and
relabelling would demand a version with reason + verified_against. A label is not behaviour, so it
lives on the `test` row and its edits land in `changelog` instead of lineage.

Vocabulary is DATA (rule 2): the engine validates shape only and never contains a label literal.
Additive column with a default — no backfill, no cardinality change, every existing read unaffected.

Revision ID: 0010_test_labels
Revises: 0009_step_video_offset
Create Date: 2026-07-26
"""
from alembic import op

revision = "0010_test_labels"
down_revision = "0009_step_video_offset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table test add column if not exists labels text[] not null default '{}'")


def downgrade() -> None:
    op.execute("alter table test drop column if exists labels")
