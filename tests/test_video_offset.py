"""step_event.video_offset_ms — ms from run-video start to each step event (migration 0007).

The run video starts at browser-context creation (Evidence.video_epoch, evidence.recording); the
interpreter stamps every emitted row with its offset from that moment. B2's two-rows-per-step means
each step gets a start offset (running row) and an end offset (terminal row) — the run-inspector's
scrubber markers ride on this single nullable column. Non-recording paths (calibration, collector
doubles) carry null. Rides the m1 gate (cold-lane evidence behavior).
"""
from __future__ import annotations

import time
from collections import defaultdict

import pytest
from sqlalchemy import text

from anneal_artifacts.store import LocalDiskStore
from anneal_db import repositories as repo
from anneal_db.engine import get_engine

from test_m1 import _wipe, seed


@pytest.fixture
def engine():
    return get_engine()


@pytest.fixture(autouse=True)
def _isolated_db():
    _wipe(get_engine())
    yield


@pytest.fixture
def artifacts(tmp_path):
    return LocalDiskStore(str(tmp_path / "artifacts"))


@pytest.mark.m1
async def test_video_offsets_populated_paired_and_monotonic(engine, artifacts):
    """A recorded suite run stamps EVERY step_event row (running + terminal) with a non-null
    video_offset_ms; per run the offsets are monotonically non-decreasing in emit order; per step
    the start offset <= end offset; and the API projection carries the field."""
    from anneal_scheduler.scheduler import run_suite
    ids = seed(engine)
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=ids["readonly_suite"],
                               target_id=ids["target_id"])
    report = await run_suite(engine, artifacts, suite_run_id=sr, target_id=ids["target_id"],
                             max_parallel=2)
    assert report.state == "done" and report.verdict == "green", report

    with engine.connect() as c:
        rows = c.execute(text("""
            select id, run_result_id, step_index, status, elapsed_ms, video_offset_ms
            from step_event order by id""")).mappings().all()
    assert rows, "no step events emitted"

    # 1. every row of a recorded run carries an offset (running AND terminal rows)
    missing = [r for r in rows if r["video_offset_ms"] is None]
    assert not missing, f"{len(missing)} step_event rows missing video_offset_ms"

    # 2. offsets never run backwards within a run (emit order == id order)
    per_run: dict = defaultdict(list)
    for r in rows:
        per_run[r["run_result_id"]].append(r["video_offset_ms"])
    for rr, offs in per_run.items():
        assert offs == sorted(offs), f"run {rr}: offsets not monotonic: {offs}"

    # 3. B2 pairing: the step's running-row offset <= its terminal-row offset, and the span
    #    roughly agrees with elapsed_ms (within generous slop — offsets are context-relative)
    by_step: dict = defaultdict(dict)
    for r in rows:
        key = (r["run_result_id"], r["step_index"])
        if r["status"] == "running":
            by_step[key]["start"] = r["video_offset_ms"]
        else:
            by_step[key]["end"] = r["video_offset_ms"]
            by_step[key]["elapsed"] = r["elapsed_ms"]
    for key, d in by_step.items():
        assert "start" in d and "end" in d, f"step {key} missing a start or terminal row"
        assert d["start"] <= d["end"], f"step {key}: start offset after end offset"
        span = d["end"] - d["start"]
        assert abs(span - (d["elapsed"] or 0)) < 2000, \
            f"step {key}: video span {span}ms disagrees wildly with elapsed {d['elapsed']}ms"

    # 4. the read projection exposes the field (REST /runs/{id}/steps + SSE ride on this)
    any_run = next(iter(per_run))
    steps = repo.get_steps(engine, run_result_id=any_run)
    assert steps and "video_offset_ms" in steps[0]


@pytest.mark.m1
def test_video_offset_null_without_recording_epoch():
    """Non-recording paths pass no video_epoch → offsets stay null (column nullable by design);
    with an epoch the offset is a small non-negative ms delta."""
    from anneal_executor.interpreter import Interpreter
    cold = Interpreter.cold(page=None, plan=None, sink=None, artifacts=None)
    assert cold._video_offset_ms() is None
    stamped = Interpreter.cold(page=None, plan=None, sink=None, artifacts=None,
                               video_epoch=time.monotonic())
    off = stamped._video_offset_ms()
    assert off is not None and 0 <= off < 1000
