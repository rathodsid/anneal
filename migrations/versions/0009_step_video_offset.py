"""step_event.video_offset_ms — ms from run-video start to each step event.

The run video begins when Playwright creates the browser context (record_video_dir); the
interpreter stamps every emitted event with its offset from that moment. Because B2 emits two
rows per step (running at start, terminal at end), each step gets a start and end offset —
the run-inspector's video scrubber markers ride on this one nullable column. Null on
non-recording paths (calibration, collector sinks). Additive: no cardinality change, D33
evidence-count invariant untouched.

Revision ID: 0009_step_video_offset
Revises: 0008_suite_run_auth_fields (re-chained from 0006 at integration — the auth merge claimed 0007/0008)
Create Date: 2026-07-16
"""
from alembic import op

revision = "0009_step_video_offset"
down_revision = "0008_suite_run_auth_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table step_event add column if not exists video_offset_ms int")


def downgrade() -> None:
    op.execute("alter table step_event drop column if exists video_offset_ms")
