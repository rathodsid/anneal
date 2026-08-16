"""run_result.role + incident_member.verify_run_id — B3 (design/BACKEND-SPEC.md)

Retry/heal/verify replays become real, addressable run_results (role column; child runs share the
parent red run's run_task_id) with unique artifact dirs — fixing the fixed-dir "reauthor-verify"
overwrite — and real step_event trails. verify_run_id links each incident member to the verify
replay that proved its provisional, so the D37 money-shot can show red-run AND verify-run evidence.

Revision ID: 0005_run_role_verify_link
Revises: 0004_one_active_run_per_target
Create Date: 2026-07-16
"""
from alembic import op

revision = "0005_run_role_verify_link"
down_revision = "0004_one_active_run_per_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table run_result add column if not exists role text not null default 'primary'")
    op.execute("alter table incident_member add column if not exists verify_run_id uuid")
    op.execute("""do $$ begin
        alter table incident_member add constraint fk_member_verify_run
          foreign key (verify_run_id) references run_result(id);
    exception when duplicate_object then null; end $$""")


def downgrade() -> None:
    op.execute("alter table incident_member drop constraint if exists fk_member_verify_run")
    op.execute("alter table incident_member drop column if exists verify_run_id")
    op.execute("alter table run_result drop column if exists role")
