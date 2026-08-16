"""`anneal export` — compile a frozen test_version to a standalone Playwright script."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import NoResultFound

from anneal_core.bundle import Bundle
from anneal_core.compile import compile as compile_bundle
from anneal_core.stepplan import CompileContext
from anneal_db import repositories as repo
from anneal_executor.export import to_script

from ..deps import Engine
from ..models import ExportResponse

router = APIRouter(tags=["export"])


@router.get("/export/{test_id}/{version}", response_model=ExportResponse)
def export(test_id: str, version: int, eng: Engine):
    try:
        raw = repo.load_bundle(eng, test_id=test_id, version=version)
    except NoResultFound:
        raw = None
    if raw is None:
        raise HTTPException(status_code=404, detail="test version not found")
    bundle = Bundle.model_validate(raw)
    modules = repo.load_modules(eng, project_id=bundle.project_id)
    ctx = CompileContext(token="exportstub", today=date.today(), date_format="%Y-%m-%d",
                         params={}, modules=modules)
    plan = compile_bundle(bundle, ctx)
    return {"test_id": test_id, "version": version, "script": to_script(plan)}
