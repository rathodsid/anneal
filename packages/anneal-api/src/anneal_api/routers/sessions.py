"""A2 read-hole: session health (sessions.html) — each persona's seeded target-app login state, so
the UI can show "re-seed needed" on a parked_auth suite. Re-seed/validate triggers are B5."""
from __future__ import annotations

from fastapi import APIRouter

from anneal_db import repositories as repo

from ..deps import Engine
from ..models import SessionHealth

router = APIRouter(tags=["sessions"])


@router.get("/projects/{project}/sessions", response_model=list[SessionHealth])
def sessions_list(project: str, eng: Engine):
    return repo.list_sessions(eng, project_id=project)
