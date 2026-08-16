"""The incident inbox (§9, D37) — read on both clients; ruling is WEB-ONLY (D32)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from anneal_db import repositories as repo

from ..deps import Engine
from ..models import Incident, IncidentDetail, RulingRequest, RulingResponse

router = APIRouter(tags=["incidents"])


@router.get("/projects/{project}/incidents", response_model=list[Incident])
def incidents_list(project: str, eng: Engine, state: str | None = Query(None),
                   klass: str | None = None, ruling: str | None = None,
                   since: datetime | None = None, until: datetime | None = None,
                   limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    # A3: pagination + klass/ruling/date filters (was state-only).
    return repo.list_incidents(eng, project_id=project, state=state, klass=klass, ruling=ruling,
                               since=since, until=until, limit=limit, offset=offset)


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
def incident_detail(incident_id: str, eng: Engine):
    inc = repo.get_incident(eng, incident_id=incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")
    inc["members"] = repo.incident_members(eng, incident_id=incident_id)
    inc["changelog"] = repo.list_changelog(eng, project_id=inc["project_id"], incident_id=incident_id)
    return inc


@router.post("/incidents/{incident_id}/ruling", response_model=RulingResponse)
def incident_ruling(incident_id: str, body: RulingRequest, eng: Engine):
    # WEB-ONLY (D32): a ruling belongs in front of screenshots/video, not a terminal — the CLI has
    # no command that reaches this. Uniform (`ruling`) or per-member (`members`, A5 Direction B) —
    # exactly one, enforced by the model (422 otherwise). One atomic transition either way.
    from anneal_classify import incidents as inc_machine
    inc = repo.get_incident(eng, incident_id=incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")
    try:
        if body.members is not None:
            return inc_machine.rule_members(
                eng, project_id=inc["project_id"], incident_id=incident_id,
                members={str(t): r for t, r in body.members.items()},
                ruling_note=body.ruling_note, ruled_by=body.ruled_by)
        return inc_machine.rule(eng, project_id=inc["project_id"], incident_id=incident_id,
                                ruling=body.ruling, ruling_note=body.ruling_note,
                                ruled_by=body.ruled_by)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
