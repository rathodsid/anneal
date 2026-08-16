"""Changelog (D37) — the project-wide audit trail the inbox and test-viewer read."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from anneal_db import repositories as repo

from ..deps import Engine
from ..models import ChangelogEntry

router = APIRouter(tags=["changelog"])


@router.get("/projects/{project}/changelog", response_model=list[ChangelogEntry])
def changelog(project: str, eng: Engine, test_id: str | None = None, kind: str | None = None,
              since: datetime | None = None,
              limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    # A3: test_id/kind/since filters + pagination (changelog.html, test-viewer › Changelog).
    return repo.list_changelog(eng, project_id=project, test_id=test_id, kind=kind, since=since,
                               limit=limit, offset=offset)
