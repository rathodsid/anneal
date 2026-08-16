"""The ONE launch path (D32/B1): saved suite, console one-off, and selection launch all create a
suite_run and spawn it — never forked behavior, never synchronous.

B1 execution model (docs/BACKEND-SPEC.md): run_suite executes on a DEDICATED THREAD with its own
asyncio loop. The DB engine is sync SQLAlchemy and the LLM client is sync anthropic (≤90s/call) —
on the API's event loop they would freeze the very poll/SSE endpoints users watch during a run.

The launch lock is the one_active_suite_run_per_target partial unique index: the suite_run INSERT
itself collides for a busy target (no check-then-insert TOCTOU) → TargetBusy → API 409.
"""
from __future__ import annotations

import asyncio
import threading
import traceback

from sqlalchemy.exc import IntegrityError

from anneal_db import repositories as repo
from anneal_scheduler.scheduler import run_suite


class TargetBusy(Exception):
    """Another suite_run holds the target's launch lock."""

    def __init__(self, active_suite_run_id: str | None):
        self.active_suite_run_id = active_suite_run_id
        super().__init__(f"target busy: active suite_run {active_suite_run_id}")


def create_and_launch(eng, art, *, project: str, suite_id: str, target_id: str,
                      mode: str = "saved", trigger: str = "manual") -> str:
    """Create the suite_run (the insert IS the lock) and spawn its thread. Returns suite_run_id
    immediately; progress via GET /suite_runs/{id} (poll) or /events (SSE)."""
    try:
        sr = repo.create_suite_run(eng, project_id=project, suite_id=suite_id, target_id=target_id,
                                   mode=mode, trigger=trigger)
    except IntegrityError:
        raise TargetBusy(repo.active_suite_run(eng, target_id=target_id)) from None
    _spawn(eng, art, suite_run_id=sr, target_id=target_id)
    return sr


def _spawn(eng, art, *, suite_run_id: str, target_id: str) -> None:
    def _run():
        try:
            asyncio.run(run_suite(eng, art, suite_run_id=suite_run_id, target_id=target_id))
        except Exception:  # noqa: BLE001 — a crashed thread must still release the launch lock
            traceback.print_exc()
            try:
                repo.set_suite_run_state(eng, suite_run_id=suite_run_id, state="parked",
                                         verdict="interrupted", park_reason="INTERRUPTED")
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    threading.Thread(target=_run, daemon=True, name=f"run_suite-{suite_run_id}").start()