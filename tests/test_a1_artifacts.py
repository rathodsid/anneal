"""A1 acceptance gate (docs/BACKEND-SPEC.md §Wave A) — artifact serving.

DONE for A1 == GET /runs/{id}/artifacts/{kind} serves the stored trace/video/screenshot bytes with the
right media type, synthesizes network_log from step_event.network, 404s on missing evidence, and 422s
on an unknown kind. Seeds a run through the real repo lifecycle — no browser needed.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from anneal_api.app import create_app
from anneal_db import repositories as repo
from anneal_db.engine import get_engine
from anneal_db.sink import DBStepEventSink

TRACE_BYTES = b"PK\x03\x04 not-really-a-zip but the bytes must round-trip"
PNG_BYTES = b"\x89PNG\r\n\x1a\n fake-png-bytes"


def _seed_run_with_artifacts(engine, tmp_path) -> str:
    """A finished run_result with a trace + screenshot on disk (no video) and one networked step."""
    target_id = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                         target_key="a1-artifacts", base_url="http://localhost:1")
    test_id, _ = repo.upsert_test(engine, project_id="golden", test_key="a1-artifact-test",
                                  bundle={"persona": "member"}, verified_against="a1-artifacts")
    suite = repo.create_suite(engine, project_id="golden", name="a1-artifacts",
                              member_test_ids=[test_id])
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=suite, target_id=target_id)
    repo.expand_run_tasks(engine, suite_run_id=sr)
    claim = repo.claim_next(engine, suite_run_id=sr, worker_id="a1")
    rr = repo.start_run_result(engine, run_task_id=claim["run_task_id"], test_id=test_id,
                               test_version=1, environment="a1-artifacts")
    trace = tmp_path / "trace.zip"
    trace.write_bytes(TRACE_BYTES)
    shot = tmp_path / "final.png"
    shot.write_bytes(PNG_BYTES)
    DBStepEventSink(engine).emit(rr, {
        "step_index": 0, "step_id": "s0", "intent": "create the campaign", "action": "click",
        "status": "passed", "network": [{"method": "POST", "url": "/api/campaigns", "status": 201,
                                         "matched_expectation": True}],
    })
    repo.finish_run_result(engine, run_result_id=rr, run_task_id=claim["run_task_id"],
                           test_id=test_id, verdict="green", trace_path=str(trace),
                           screenshot_path=str(shot), duration_ms=1234)
    # terminal state releases the B1 one-active-suite_run-per-target launch lock for re-runs
    repo.set_suite_run_state(engine, suite_run_id=sr, state="done", verdict="green")
    return rr


@pytest.mark.api_contract
def test_artifact_serving_end_to_end(tmp_path):
    engine = get_engine()
    rr = _seed_run_with_artifacts(engine, tmp_path)
    with TestClient(create_app(engine=engine)) as tc:
        r = tc.get(f"/runs/{rr}/artifacts/trace")
        assert r.status_code == 200 and r.content == TRACE_BYTES
        assert r.headers["content-type"] == "application/zip"

        r = tc.get(f"/runs/{rr}/artifacts/screenshot")
        assert r.status_code == 200 and r.content == PNG_BYTES
        assert r.headers["content-type"] == "image/png"

        assert tc.get(f"/runs/{rr}/artifacts/video").status_code == 404      # never written for this run

        r = tc.get(f"/runs/{rr}/artifacts/network_log")                      # synthesized from step_event
        assert r.status_code == 200
        entries = r.json()
        assert entries == [{"step_index": 0, "method": "POST", "url": "/api/campaigns",
                            "status": 201, "matched_expectation": True}]

        assert tc.get(f"/runs/{rr}/artifacts/har").status_code == 422        # unknown kind = schema 422
        assert tc.get(f"/runs/{uuid.uuid4()}/artifacts/trace").status_code == 404
