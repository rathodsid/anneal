"""Run reads (M1: list, detail + live step_events, D33), artifact serving (A1), and change-driven
selection (§11, M4)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from anneal_db import repositories as repo

from ..deps import Artifacts, Engine
from ..models import (PinRequest, PinResponse, RunDetail, RunListItem, SelectRequest,
                      SelectResponse, StepEvent)
from ..suites_launch import TargetBusy, create_and_launch

router = APIRouter(tags=["runs"])

# A1 evidence serving — kind → (run_result column, media type). Playwright trace is a zip (feed it to
# the trace viewer); network_log is the captured passive log (falls back to synthesizing from
# step_event.network for runs recorded before capture existed).
_ARTIFACT_FILES = {"trace": ("trace_path", "application/zip"),
                   "video": ("video_path", "video/webm"),
                   "screenshot": ("screenshot_path", "image/png")}
ArtifactKind = Literal["trace", "video", "screenshot", "network_log"]


def _assign_steps(entries: list[dict], steps: list[dict]) -> list[dict]:
    """Correlate each captured request to the step that was active when it STARTED — the step whose
    start offset is the greatest one ≤ the request's video offset. Requests before the first step
    (SPA hydration, the opening nav) map to step_index=None. Post-hoc, so capture stays decoupled
    from the interpreter; runs without offsets (non-recorded paths) just get None."""
    starts: dict[int, int] = {}
    for s in steps:
        o = s.get("video_offset_ms")
        if o is None:
            continue
        i = s["step_index"]
        starts[i] = min(starts.get(i, o), o)          # a step's earliest event = its start marker
    windows = sorted(starts.items(), key=lambda kv: kv[1])
    for e in entries:
        o = e.get("video_offset_ms")
        idx = None
        if o is not None:
            for si, start in windows:
                if start <= o:
                    idx = si
                else:
                    break
        e["step_index"] = idx
    return entries


@router.post("/projects/{project}/runs/select", response_model=SelectResponse,
             responses={409: {"description": "launch requested but the target is busy"}})
def select_run(project: str, body: SelectRequest, eng: Engine, art: Artifacts,
               target_id: str = Query(...)):
    # §11 change-driven selection: an NL change → per-test score + human-readable reason (preview).
    # `launch: true` runs the SELECTED tests as a selection suite_run (D21 mode='selection'),
    # async (B1) — `run.verdict` is null until terminal; poll /suite_runs/{id}.
    from anneal_classify.selection import select as select_tests
    picks = select_tests(eng, project_id=project, change_nl=body.change)
    selected = [p["test_id"] for p in picks if p["selected"]]
    out = {"change": body.change, "selection": picks, "selected": selected, "run": None}
    if body.launch and selected:
        suite = repo.create_suite(eng, project_id=project, name="selection-run", member_test_ids=selected)
        try:
            sr = create_and_launch(eng, art, project=project, suite_id=suite,
                                   target_id=target_id, mode="selection", trigger="api")
        except TargetBusy as e:
            raise HTTPException(status_code=409, detail={
                "error": "target busy", "active_suite_run_id": e.active_suite_run_id})
        out["run"] = {"suite_run_id": sr, "verdict": None}
    return out


@router.get("/projects/{project}/runs", response_model=list[RunListItem])
def runs(project: str, eng: Engine, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
         test_id: str | None = None, verdict: str | None = None, environment: str | None = None,
         pinned: bool | None = None, trigger: str | None = None,
         since: datetime | None = None, until: datetime | None = None,
         role: str = "primary"):
    # A3: the Runs-list filters (runs-list.html) — all optional, combined with AND.
    # B3: role defaults to 'primary' so run history stays clean; role='any' or a specific
    # retry|heal|verify surfaces the child runs (e.g. the money-shot's verify evidence).
    return repo.list_runs(eng, project_id=project, limit=limit, offset=offset, test_id=test_id,
                          verdict=verdict, environment=environment, pinned=pinned, trigger=trigger,
                          since=since, until=until, role=role)


@router.get("/runs/{run_result_id}", response_model=RunDetail)
def run_detail(run_result_id: str, eng: Engine):
    d = repo.get_run(eng, run_result_id=run_result_id)
    if not d:
        raise HTTPException(status_code=404, detail="run not found")
    d["steps"] = repo.get_steps(eng, run_result_id=run_result_id)
    return d


@router.get("/runs/{run_result_id}/steps", response_model=list[StepEvent])
def run_steps(run_result_id: str, eng: Engine, after: int = Query(0, ge=0)):
    # B1 poll tier: ?after={id} (StepEvent.id, monotonic) returns only rows newer than the cursor;
    # the client polls until run_result.verdict is terminal.
    return repo.get_steps(eng, run_result_id=run_result_id, after=after)


@router.get("/runs/{run_result_id}/events",
            responses={200: {"content": {"text/event-stream": {}},
                             "description": "SSE: one `step` event per new step_event row, then a "
                                            "`terminal` event with the final verdict."}})
async def run_events(run_result_id: str, eng: Engine, after: int = 0):
    # B1 SSE tier: tail new step_event rows by id (~250ms server poll) until the verdict is terminal.
    # Brief sync selects are laptop-scale fine — the heavy work runs on the launch thread (B1).
    if not repo.get_run(eng, run_result_id=run_result_id):
        raise HTTPException(status_code=404, detail="run not found")

    async def gen():
        cursor = after or 0
        while True:
            for row in repo.get_steps(eng, run_result_id=run_result_id, after=cursor):
                cursor = row["id"]
                yield f"event: step\ndata: {json.dumps(row, default=str)}\n\n"
            run = repo.get_run(eng, run_result_id=run_result_id)
            if run["verdict"] != "running":
                terminal = {"verdict": run["verdict"], "error_class": run["error_class"],
                            "duration_ms": run["duration_ms"]}
                yield f"event: terminal\ndata: {json.dumps(terminal, default=str)}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/runs/{run_result_id}/pin", response_model=PinResponse)
def run_pin(run_result_id: str, body: PinRequest, eng: Engine):
    # A4: reviewer pin/unpin — keep specific evidence beyond the D12 auto-pin (prune exempts pinned).
    if not repo.set_pinned(eng, run_result_id=run_result_id, pinned=body.pinned):
        raise HTTPException(status_code=404, detail="run not found")
    return {"id": run_result_id, "pinned": body.pinned}


@router.get(
    "/runs/{run_result_id}/artifacts/{kind}",
    responses={200: {"content": {"application/zip": {}, "video/webm": {}, "image/png": {},
                                 "application/json": {"schema": {"type": "array", "items": {"type": "object"}}}},
                     "description": "The artifact bytes (trace zip / webm / png), or the synthesized "
                                    "network log entries as JSON."}},
)
def run_artifact(run_result_id: str, kind: ArtifactKind, eng: Engine):
    # A1 (docs/BACKEND-SPEC.md): the ONE evidence serve path. No user input reaches the filesystem —
    # `kind` is a closed enum (422 otherwise) and the path comes from the run row the engine wrote, so
    # traversal is structurally impossible. Behind the ArtifactStore Protocol the MinIO swap later
    # returns a presigned redirect instead of streaming bytes — no contract change.
    d = repo.get_run(eng, run_result_id=run_result_id)
    if not d:
        raise HTTPException(status_code=404, detail="run not found")
    if kind == "network_log":
        # Prefer the captured passive log (full traffic + timing), correlated to the active step.
        path = d.get("network_log_path")
        if path and Path(path).is_file():
            entries = json.loads(Path(path).read_text())
            steps = repo.get_steps(eng, run_result_id=run_result_id)
            return JSONResponse(_assign_steps(entries, steps))
        # Fallback for runs recorded before capture existed: synthesize from step_event.network
        # (armed expectations only — sparse, no timing), so old runs still return something.
        steps = repo.get_steps(eng, run_result_id=run_result_id)
        entries = [{"step_index": s["step_index"], **n} for s in steps for n in (s["network"] or [])]
        return JSONResponse(entries)
    col, media = _ARTIFACT_FILES[kind]
    path = d.get(col)
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail=f"no {kind} for this run")
    return FileResponse(path, media_type=media)
