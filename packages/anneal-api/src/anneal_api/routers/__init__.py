"""APIRouters (A0) — one per resource. app.create_app includes them all; each resolves the engine and
artifact store via deps.py so behavior stays identical to the pre-router monolith."""
from __future__ import annotations

from . import changelog, export, incidents, labels, runs, sessions, suites, targets, tests

ALL = [suites.router, tests.router, runs.router, incidents.router, changelog.router, export.router,
       targets.router, sessions.router, labels.router]
