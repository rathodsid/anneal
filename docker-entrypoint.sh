#!/bin/sh
# Optional on-boot migration. ARCHITECTURE.md §7 keeps migrations OUT of the app boot by default
# (bad/slow migration shouldn't gate readiness; multi-replica races). For a single-pod deploy where
# a separate migrate Job is overkill, set ANNEAL_MIGRATE_ON_BOOT=1 to run `alembic upgrade head`
# (idempotent) before the server starts. Unset → original behaviour, exec the CMD unchanged.
set -e
if [ -n "$ANNEAL_MIGRATE_ON_BOOT" ]; then
  echo "[anneal] ANNEAL_MIGRATE_ON_BOOT set — alembic upgrade head"
  uv run alembic upgrade head
fi
exec "$@"
