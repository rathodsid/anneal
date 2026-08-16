"""B2 acceptance gates (docs/BACKEND-SPEC.md §Wave B) — step-started events, the live grain.

DONE for B2 == the DB sink writes a 'running' row at step START and the terminal row at completion
(TWO rows, never an update — a mutated row is invisible to the id-cursor poll/SSE tail); the D33
completeness count excludes 'running' rows so a pass can never finalize over started-but-unfinished
steps; collector sinks (no emit_started) still see exactly one event per step.

The full-integration proof is the m1 gate itself: a real suite run through the async API reaches
verdict=green — impossible if the doubled rows broke the D33 finalizer.
"""
from __future__ import annotations

import pytest

from anneal_db import repositories as repo
from anneal_db.engine import get_engine
from anneal_db.sink import DBStepEventSink


def _seed_run(engine, key: str) -> str:
    target_id = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                         target_key=key, base_url="http://localhost:1")
    test_id, _ = repo.upsert_test(engine, project_id="golden", test_key=f"{key}-test",
                                  bundle={"persona": "member"}, verified_against=key)
    suite = repo.create_suite(engine, project_id="golden", name=key, member_test_ids=[test_id])
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=suite, target_id=target_id)
    repo.expand_run_tasks(engine, suite_run_id=sr)
    claim = repo.claim_next(engine, suite_run_id=sr, worker_id="b2")
    rr = repo.start_run_result(engine, run_task_id=claim["run_task_id"], test_id=test_id,
                               test_version=1, environment=key)
    repo.set_suite_run_state(engine, suite_run_id=sr, state="done", verdict="green")  # release lock
    return rr


@pytest.mark.api_contract
def test_two_rows_per_step_and_d33_counts_terminal_only():
    engine = get_engine()
    rr = _seed_run(engine, "b2-grain")
    sink = DBStepEventSink(engine)

    # step 0: started then passed; step 1: started then NEVER completed (mid-run crash shape)
    sink.emit_started(rr, {"step_index": 0, "step_id": "s0", "intent": "open the page",
                           "action": "goto", "budget_ms": 8000})
    sink.emit(rr, {"step_index": 0, "step_id": "s0", "action": "goto", "status": "passed",
                   "elapsed_ms": 120})
    sink.emit_started(rr, {"step_index": 1, "step_id": "s1", "intent": "click save",
                           "action": "click", "budget_ms": 8000})

    rows = repo.get_steps(engine, run_result_id=rr)
    assert [(r["step_index"], r["status"]) for r in rows] == \
           [(0, "running"), (0, "passed"), (1, "running")]        # id-ordered, pairs by step_index
    assert rows[0]["id"] < rows[1]["id"] < rows[2]["id"]          # cursor-safe: inserts, no updates
    assert rows[2]["budget_ms"] == 8000 and rows[2]["elapsed_ms"] is None

    # D33: the completeness gate must see ONE terminal event, not three rows —
    # step 1 started-but-unfinished must NOT count toward a pass verdict
    assert repo.count_step_events(engine, run_result_id=rr) == 1

    # the id cursor delivers the started row like any other event (live poll/SSE tier)
    after_first = rows[0]["id"]
    newer = repo.get_steps(engine, run_result_id=rr, after=after_first)
    assert [(r["step_index"], r["status"]) for r in newer] == [(0, "passed"), (1, "running")]


@pytest.mark.api_contract
def test_collector_sinks_unaffected():
    """A sink without emit_started (calibration/reauthor collectors, test doubles) gets exactly the
    completed-event stream — the interpreter's duck-type check must not require the method."""
    from anneal_executor.interpreter import Interpreter

    class Collector:
        def __init__(self):
            self.events = []

        def emit(self, rr, event):
            self.events.append(event)

    # _emit_started must be a no-op against a collector (no AttributeError, no extra event)
    interp = object.__new__(Interpreter)          # no browser needed to exercise the emit path
    interp.sink = Collector()
    interp.run_result_id = "b2-collector"

    class Step:
        index, step_id, intent, action, budget_ms = 0, "s0", "x", "click", 1000

    interp._emit_started(Step())
    assert interp.sink.events == []               # collectors see completed events only

    db_like_events = []

    class DBLike:
        def emit(self, rr, event):
            db_like_events.append(event)

        def emit_started(self, rr, event):
            db_like_events.append({**event, "status": "running"})

    interp.sink = DBLike()
    interp._emit_started(Step())
    assert db_like_events == [{"step_index": 0, "step_id": "s0", "intent": "x", "action": "click",
                               "budget_ms": 1000, "status": "running"}]
