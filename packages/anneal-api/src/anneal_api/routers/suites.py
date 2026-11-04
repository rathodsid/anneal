"""Suite launch (M1/B1: async, 409-locked) + the A2 suite/suite_run reads + suite-level SSE.
D21: the same launch endpoint a CI job (deploy_hook) or the console (manual) calls — never cron
(the Trigger Literal enforces it as a 422)."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from anneal_db import repositories as repo

from ..deps import Artifacts, Engine
from ..models import LaunchAccepted, Suite, SuiteCreate, SuitePatch, SuiteRunDetail, Trigger
from ..suites_launch import TargetBusy, create_and_launch

router = APIRouter(tags=["suites"])

_TERMINAL = ("done", "parked")


@router.post("/projects/{project}/suites/{suite_id}/runs", response_model=LaunchAccepted,
             status_code=202,
             responses={409: {"description": "target busy — body carries the active suite_run_id"}})
def launch(project: str, suite_id: str, eng: Engine, art: Artifacts,
           target_id: str = Query(...),
           trigger: Trigger = Query("api")):
    # B1: returns immediately; run_suite executes on its own thread+loop. Watch via
    # GET /suite_runs/{id} (poll) or /events (SSE).
    try:
        sr = create_and_launch(eng, art, project=project, suite_id=suite_id,
                               target_id=target_id, trigger=trigger)
    except TargetBusy as e:
        raise HTTPException(status_code=409, detail={
            "error": "target busy", "active_suite_run_id": e.active_suite_run_id})
    return {"suite_run_id": sr}


@router.get("/suite_runs/{suite_run_id}/events",
            responses={200: {"content": {"text/event-stream": {}},
                             "description": "SSE: `step` events across all member runs (tagged "
                                            "test_key/run_result_id) until a `terminal` event."}})
async def suite_run_events(suite_run_id: str, eng: Engine, after: int = 0):
    # B1 SSE: tail new step_event rows by id (~250ms server poll), multiplexing per-test progress.
    # The brief sync selects are laptop-scale fine — the heavy work runs on the launch thread.
    if not repo.get_suite_run(eng, suite_run_id=suite_run_id):
        raise HTTPException(status_code=404, detail="suite run not found")

    async def gen():
        cursor = after or 0
        while True:
            # sync repo calls go to a worker thread (see runs.run_events)
            for row in await asyncio.to_thread(repo.suite_run_step_events_after, eng,
                                               suite_run_id=suite_run_id, after=cursor):
                cursor = row["id"]
                yield f"event: step\ndata: {json.dumps(row, default=str)}\n\n"
            sr = await asyncio.to_thread(repo.get_suite_run, eng, suite_run_id=suite_run_id)
            if sr["state"] in _TERMINAL:
                sr["results"] = await asyncio.to_thread(repo.suite_run_results, eng, suite_run_id=suite_run_id)
                yield f"event: terminal\ndata: {json.dumps(sr, default=str)}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ── A4: suite CRUD — the suite.html membership modal ─────────────────────────────────────────────
@router.post("/projects/{project}/suites", response_model=Suite)
def suite_create(project: str, body: SuiteCreate, eng: Engine):
    # upserts on (project, name) — same semantic the launch paths rely on
    sid = repo.create_suite(eng, project_id=project, name=body.name,
                            member_test_ids=[str(t) for t in body.member_test_ids])
    return repo.get_suite(eng, suite_id=sid)


@router.patch("/suites/{suite_id}", response_model=Suite)
def suite_patch(suite_id: str, body: SuitePatch, eng: Engine):
    members = [str(t) for t in body.member_test_ids] if body.member_test_ids is not None else None
    if not repo.update_suite(eng, suite_id=suite_id, name=body.name, member_test_ids=members):
        raise HTTPException(status_code=404, detail="suite not found")
    return repo.get_suite(eng, suite_id=suite_id)


# ── A2 read-holes: suites were create-only; suite_runs had no post-hoc read at all ───────────────
@router.get("/projects/{project}/suites", response_model=list[Suite])
def suites_list(project: str, eng: Engine):
    return repo.list_suites(eng, project_id=project)


@router.get("/suites/{suite_id}", response_model=Suite)
def suite_detail(suite_id: str, eng: Engine):
    s = repo.get_suite(eng, suite_id=suite_id)
    if not s:
        raise HTTPException(status_code=404, detail="suite not found")
    return s


@router.get("/suite_runs/{suite_run_id}", response_model=SuiteRunDetail)
def suite_run_detail(suite_run_id: str, eng: Engine):
    # Post-hoc inspection — and the terminal-state poll target B1's async CLI/console depend on.
    sr = repo.get_suite_run(eng, suite_run_id=suite_run_id)
    if not sr:
        raise HTTPException(status_code=404, detail="suite run not found")
    sr["results"] = repo.suite_run_results(eng, suite_run_id=suite_run_id)
    return sr
