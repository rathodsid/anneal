"""B1 acceptance gates (docs/BACKEND-SPEC.md §Wave B) — async launch + live progress.

DONE for B1 == launch returns 202 {suite_run_id} immediately and the run completes on its own
thread; a busy target 409s with the active id (index-enforced, no TOCTOU); a restart-orphaned run
is reaped INTERRUPTED at startup; steps poll with an id cursor; SSE streams steps then a terminal
event. Uses an EMPTY suite for launch-path tests (parks EMPTY_SUITE without a browser — fast).
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from anneal_api.app import create_app
from anneal_db import repositories as repo
from anneal_db.engine import get_engine
from anneal_db.sink import DBStepEventSink


def _seed_target_and_suite(engine, key: str, members=()) -> dict:
    target_id = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                         target_key=key, base_url="http://localhost:1")
    suite = repo.create_suite(engine, project_id="golden", name=key, member_test_ids=list(members))
    return {"target_id": target_id, "suite": suite}


def _poll_terminal(tc, sr_id: str, timeout_s: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        d = tc.get(f"/suite_runs/{sr_id}").json()
        if d["state"] in ("done", "parked"):
            return d
        time.sleep(0.1)
    raise AssertionError(f"suite_run {sr_id} not terminal after {timeout_s}s: {d}")


@pytest.mark.api_contract
def test_launch_returns_202_immediately_and_run_completes():
    engine = get_engine()
    ids = _seed_target_and_suite(engine, "b1-async")
    with TestClient(create_app(engine=engine)) as tc:
        t0 = time.monotonic()
        r = tc.post(f"/projects/golden/suites/{ids['suite']}/runs",
                    params={"target_id": ids["target_id"], "trigger": "manual"})
        elapsed = time.monotonic() - t0
        assert r.status_code == 202, r.text
        assert elapsed < 2.0, f"launch blocked for {elapsed:.1f}s — not async"
        sr = _poll_terminal(tc, r.json()["suite_run_id"])
        # empty suite must not pass a deploy gate vacuously (D29) — parks EMPTY_SUITE off-thread
        assert (sr["state"], sr["verdict"], sr["park_reason"]) == \
               ("parked", "parked_env", "EMPTY_SUITE")


@pytest.mark.api_contract
def test_busy_target_409_with_active_id_and_releases_on_terminal():
    engine = get_engine()
    ids = _seed_target_and_suite(engine, "b1-busy")
    with TestClient(create_app(engine=engine)) as tc:
        # hold the launch lock AFTER startup (the startup reaper would park a pre-existing active)
        active = repo.create_suite_run(engine, project_id="golden", suite_id=ids["suite"],
                                       target_id=ids["target_id"])
        r = tc.post(f"/projects/golden/suites/{ids['suite']}/runs",
                    params={"target_id": ids["target_id"]})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["active_suite_run_id"] == active
        # terminal state releases the lock → next launch is accepted
        repo.set_suite_run_state(engine, suite_run_id=active, state="done", verdict="green")
        r2 = tc.post(f"/projects/golden/suites/{ids['suite']}/runs",
                     params={"target_id": ids["target_id"]})
        assert r2.status_code == 202, r2.text
        _poll_terminal(tc, r2.json()["suite_run_id"])


@pytest.mark.api_contract
def test_startup_reaper_marks_interrupted():
    engine = get_engine()
    ids = _seed_target_and_suite(engine, "b1-reaper")
    orphan = repo.create_suite_run(engine, project_id="golden", suite_id=ids["suite"],
                                   target_id=ids["target_id"])
    repo.set_suite_run_state(engine, suite_run_id=orphan, state="running")
    with TestClient(create_app(engine=engine)) as tc:      # startup runs the reaper
        d = tc.get(f"/suite_runs/{orphan}").json()
        assert (d["state"], d["verdict"], d["park_reason"]) == \
               ("parked", "interrupted", "INTERRUPTED"), d


@pytest.mark.api_contract
def test_steps_cursor_and_sse_stream():
    engine = get_engine()
    ids = _seed_target_and_suite(engine, "b1-cursor")
    test_id, _ = repo.upsert_test(engine, project_id="golden", test_key="b1-cursor-test",
                                  bundle={"persona": "member"}, verified_against="b1-cursor")
    repo.update_suite(engine, suite_id=ids["suite"], member_test_ids=[test_id])
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=ids["suite"],
                               target_id=ids["target_id"])
    repo.expand_run_tasks(engine, suite_run_id=sr)
    claim = repo.claim_next(engine, suite_run_id=sr, worker_id="b1")
    rr = repo.start_run_result(engine, run_task_id=claim["run_task_id"], test_id=test_id,
                               test_version=1, environment="b1-cursor")
    sink = DBStepEventSink(engine)
    for i in range(3):
        sink.emit(rr, {"step_index": i, "step_id": f"s{i}", "action": "click", "status": "passed"})
    repo.finish_run_result(engine, run_result_id=rr, run_task_id=claim["run_task_id"],
                           test_id=test_id, verdict="green", duration_ms=9)
    repo.set_suite_run_state(engine, suite_run_id=sr, state="done", verdict="green")

    with TestClient(create_app(engine=engine)) as tc:
        # poll tier: id cursor returns only rows after it
        steps = tc.get(f"/runs/{rr}/steps").json()
        assert [s["step_index"] for s in steps] == [0, 1, 2] and all(s["id"] for s in steps)
        after = steps[0]["id"]
        newer = tc.get(f"/runs/{rr}/steps", params={"after": after}).json()
        assert [s["step_index"] for s in newer] == [1, 2]

        # SSE tier (run): terminal run streams its steps then the terminal event and closes
        with tc.stream("GET", f"/runs/{rr}/events") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(resp.iter_text())
        assert body.count("event: step") == 3
        assert "event: terminal" in body and '"verdict": "green"' in body

        # SSE tier (suite): multiplexed step events tagged with test_key, then suite terminal
        with tc.stream("GET", f"/suite_runs/{sr}/events") as resp:
            body = "".join(resp.iter_text())
        assert body.count("event: step") == 3 and "b1-cursor-test" in body
        assert "event: terminal" in body and '"state": "done"' in body
