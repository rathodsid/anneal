"""A2 read-holes: target list (suite target selector) + TerritoryNotes (D25 recon output —
written on authoring, previously zero read path). Plus the auth/multi-platform onboarding surface
(design §3.6): POST a target + optional credential in one call (the `anneal onboard` CLI mirrors it)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from anneal_db import repositories as repo

from ..deps import Engine
from ..models import Target, TerritoryNotes

router = APIRouter(tags=["targets"])


class OnboardTargetRequest(BaseModel):
    key: str
    base_url: str
    auth: dict | None = None                 # {"kind": clerk|form|auto|none, "creds_ref"?, "signin_path"?}
    probe_path: str | None = None
    persona: str = "member"
    project_name: str | None = None
    timezone: str = "UTC"
    username: str | None = None              # optional — the env layers may cover creds instead
    password: str | None = None


class OnboardTargetResponse(BaseModel):
    target_id: str
    project: str
    key: str
    auth: dict
    credential_stored: bool


@router.post("/projects/{project}/targets", response_model=OnboardTargetResponse)
def onboard_target(project: str, body: OnboardTargetRequest, eng: Engine):
    # Onboarding a platform = ONE call (design §3.6): upsert the target (profile.auth block) +
    # optionally encrypt/store the credential. Seeds NO session — the first probe/login happens lazily
    # on first run (a target is usable once its profile + any cred layer exist). The CLI `anneal onboard` mirrors this exactly (D32, no forked behavior).
    from anneal_db import vault
    profile = {"auth": body.auth or {"kind": "none"}}
    if body.probe_path:
        profile["probe_path"] = body.probe_path
    tid = repo.seed_project_target(eng, project_id=project, project_name=body.project_name or project,
                                   target_key=body.key, base_url=body.base_url,
                                   timezone_=body.timezone, profile=profile)
    stored = False
    if body.username and body.password:
        try:
            vault.store_credential(eng, project_id=project, persona=body.persona,
                                   username=body.username, password=body.password)
            stored = True
        except vault.VaultKeyMissing as e:                     # refuse-without-key names the var (§3.3)
            raise HTTPException(status_code=400, detail=str(e))
    return OnboardTargetResponse(target_id=tid, project=project, key=body.key, auth=profile["auth"],
                                 credential_stored=stored)


@router.get("/projects/{project}/targets", response_model=list[Target])
def targets_list(project: str, eng: Engine):
    return repo.list_targets(eng, project_id=project)


@router.get("/targets/{target_id}/territory", response_model=TerritoryNotes)
def territory(target_id: str, eng: Engine):
    notes = repo.get_territory_notes(eng, target_id=target_id)
    if not notes:
        raise HTTPException(status_code=404, detail="no territory notes for this target")
    return notes
