"""Authoring (D24/D26), console one-off run, the A2 test-catalog/lineage reads, and the B4 golden
reference recording (per-test 'how it should look' video)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from anneal_db import repositories as repo

from ..deps import Artifacts, Engine
from ..models import (AuthorError, AuthorFrozen, AuthorNeedsAnswers, AuthorRequest, AuthorResponse,
                      Golden, LaunchAccepted, TestDetail, TestListItem, TestPatch, TestVersion,
                      TestVersionListItem, normalize_labels)
from ..suites_launch import TargetBusy, create_and_launch

router = APIRouter(tags=["tests"])


@router.post("/projects/{project}/tests", response_model=AuthorResponse)
async def author(project: str, body: AuthorRequest, eng: Engine, art: Artifacts,
                 test_key: str = Query(...), target_id: str = Query(...),
                 labels: list[str] = Query(default=[])):
    # D24: author a test from NL. If the ambiguity interview (D26) has open questions, returns
    # {status:"needs_answers", questions:[...]} — the caller answers and re-POSTs.
    # B4: passing `art` opts a successful freeze into the golden auto-seed (clean replay recording).
    #
    # Authoring drives a real browser + the model and can run for minutes — past the ~100s a fronting
    # proxy (Cloudflare 524) allows a silent origin. So the response is STREAMED: whitespace heartbeats
    # flush while the work runs (leading whitespace is valid JSON, so the client parses the concatenated
    # body unchanged), then the terminal JSON. The status line is committed before the outcome is known,
    # so a failure is delivered in-band as {status:"error", detail} (AuthorError), not a 5xx.
    from anneal_authoring.service import author_and_freeze

    # Validate labels BEFORE the status line is committed — a malformed label is a 422, not an in-band
    # error after a minute of browser work. Labels are caller-supplied only: the author prompts are
    # never taught the vocabulary (rule 2 / check_prompts_agnostic), and they never block a freeze
    # (D26 blocks on unanswered interview questions; a missing label is not one).
    try:
        want_labels = normalize_labels(labels)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None

    async def stream():
        # Own thread + loop (as suites_launch does): the LLM seam is SYNC (anthropic/openai clients,
        # up to 90s × retries) and would freeze uvicorn's loop — stalling every poll/SSE endpoint and
        # the very heartbeat this stream exists to send.
        task = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(author_and_freeze(
            eng, project_id=project, test_key=test_key, nl=body.nl, target_id=target_id,
            persona=body.persona, answers=body.answers, artifacts=art))))
        yield b" "  # flush headers + first byte immediately so the proxy sees the response start
        while True:
            done, _ = await asyncio.wait({task}, timeout=15)
            if done:
                break
            yield b" "  # heartbeat: keep the connection alive through the long author
        try:
            result = task.result()
            if want_labels and result["status"] == "frozen":
                repo.set_test_labels(eng, test_id=result["test_id"], labels=want_labels)
            model = AuthorNeedsAnswers if result["status"] == "needs_answers" else AuthorFrozen
            payload: AuthorResponse = model.model_validate(result)
        except Exception as e:  # noqa: BLE001 — any author failure becomes an in-band AuthorError
            payload = AuthorError(status="error", detail=str(e))
        yield payload.model_dump_json().encode()

    return StreamingResponse(stream(), media_type="application/json")


@router.post("/projects/{project}/tests/{test_id}/run", response_model=LaunchAccepted,
             status_code=202,
             responses={409: {"description": "target busy — body carries the active suite_run_id"}})
def run_test(project: str, test_id: str, eng: Engine, art: Artifacts,
             target_id: str = Query(...)):
    # Console convenience: wrap one authored test in a one-off suite and launch it (same async
    # run path as a saved suite / deploy hook — no forked behavior, D32/B1).
    suite = repo.create_suite(eng, project_id=project, name="ui-run", member_test_ids=[test_id])
    try:
        sr = create_and_launch(eng, art, project=project, suite_id=suite,
                               target_id=target_id, mode="saved", trigger="manual")
    except TargetBusy as e:
        raise HTTPException(status_code=409, detail={
            "error": "target busy", "active_suite_run_id": e.active_suite_run_id})
    return {"suite_run_id": sr}


# ── A2 read-holes: catalog + version lineage (test-viewer) ───────────────────────────────────────
@router.get("/projects/{project}/tests", response_model=list[TestListItem])
def tests_list(project: str, eng: Engine, label: str | None = Query(None)):
    # `label=` (empty) is the Unlabelled bucket, distinct from omitting the param entirely.
    return repo.list_tests(eng, project_id=project, label=label)


@router.get("/tests/{test_id}", response_model=TestDetail)
def test_detail(test_id: str, eng: Engine):
    t = repo.get_test(eng, test_id=test_id)
    if not t:
        raise HTTPException(status_code=404, detail="test not found")
    return t


@router.patch("/tests/{test_id}", response_model=TestDetail)
def test_patch(test_id: str, body: TestPatch, eng: Engine):
    """Catalog metadata only. Relabelling is NOT a behaviour change, so it writes a changelog row and
    never mints a test_version — lineage stays about the artifact."""
    if body.labels is not None and not repo.set_test_labels(eng, test_id=test_id, labels=body.labels):
        raise HTTPException(status_code=404, detail="test not found")
    t = repo.get_test(eng, test_id=test_id)
    if not t:
        raise HTTPException(status_code=404, detail="test not found")
    return t


@router.get("/tests/{test_id}/versions", response_model=list[TestVersionListItem])
def test_versions(test_id: str, eng: Engine):
    return repo.list_test_versions(eng, test_id=test_id)


@router.get("/tests/{test_id}/versions/{version}", response_model=TestVersion)
def test_version(test_id: str, version: int, eng: Engine):
    v = repo.get_test_version(eng, test_id=test_id, version=version)
    if not v:
        raise HTTPException(status_code=404, detail="test version not found")
    return v


# ── B4: golden reference recording (test-viewer golden panel) ────────────────────────────────────
@router.get("/tests/{test_id}/golden", response_model=Golden)
def golden_meta(test_id: str, eng: Engine):
    g = repo.get_golden(eng, test_id=test_id)
    if not g:
        raise HTTPException(status_code=404, detail="no golden recording for this test")
    return g


@router.get("/tests/{test_id}/golden/video",
            responses={200: {"content": {"video/webm": {}}}, 404: {"description": "no golden"}})
def golden_video(test_id: str, eng: Engine):
    g = repo.get_golden(eng, test_id=test_id)
    if not g or not Path(g["video_path"]).is_file():
        raise HTTPException(status_code=404, detail="no golden recording for this test")
    return FileResponse(g["video_path"], media_type="video/webm")


@router.post("/tests/{test_id}/golden", response_model=Golden)
async def golden_upload(test_id: str, eng: Engine, art: Artifacts, file: UploadFile):
    # A reviewer's own reference clip OVERRIDES the auto-seed (source='manual'). Stored in the golden
    # slot; a real replace overwrites the prior bytes. reflects_version = the test's current head.
    if not repo.get_test(eng, test_id=test_id):
        raise HTTPException(status_code=404, detail="test not found")
    name = "golden-upload.webm" if (file.filename or "").endswith(".webm") else "golden-upload.bin"
    path = art.put(f"golden/{test_id}", name, await file.read())
    head = repo.get_test(eng, test_id=test_id)["head_version"]
    repo.set_golden(eng, test_id=test_id, video_path=path, source="manual", reflects_version=head)
    return repo.get_golden(eng, test_id=test_id)


@router.delete("/tests/{test_id}/golden", status_code=204)
def golden_delete(test_id: str, eng: Engine):
    if not repo.clear_golden(eng, test_id=test_id):
        raise HTTPException(status_code=404, detail="no golden recording for this test")
