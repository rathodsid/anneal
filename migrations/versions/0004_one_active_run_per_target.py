"""one_active_suite_run_per_target — the B1 launch lock (design/BACKEND-SPEC.md §B1)

Async launch makes concurrent POSTs to the same target trivially possible; a naive "is one running?"
check is a TOCTOU race. This partial unique index makes the suite_run INSERT itself the lock: the
second launch collides and the API returns 409 with the active suite_run_id. Terminal states
('done'/'parked') release it; the startup reaper releases crashed runs (park_reason INTERRUPTED).

Revision ID: 0004_one_active_run_per_target
Revises: 0003_incident_member_ruling
Create Date: 2026-07-16
"""
from alembic import op

revision = "0004_one_active_run_per_target"
down_revision = "0003_incident_member_ruling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Any pre-existing stale actives would break index creation — reap them exactly as the startup
    # reaper does (this migration IS the first reap).
    op.execute("""
        update run_result set verdict='interrupted', error_class='INTERRUPTED', finished_at=now()
        where verdict='running' and run_task_id in (
          select rt.id from run_task rt join suite_run sr on sr.id = rt.suite_run_id
          where sr.state in ('pending','probing','running'))""")
    op.execute("""
        update suite_run set state='parked', verdict='interrupted', park_reason='INTERRUPTED'
        where state in ('pending','probing','running')""")
    op.execute("""
        create unique index if not exists one_active_suite_run_per_target on suite_run (target_id)
        where state in ('pending','probing','running')""")


def downgrade() -> None:
    op.execute("drop index if exists one_active_suite_run_per_target")
