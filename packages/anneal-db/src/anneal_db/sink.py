"""DB-backed StepEventSink (D33) — the interpreter writes one step_event row as each step completes.

No events.jsonl, no ingest hop: classification queries rows, the console reads them live, a mid-run
crash leaves exactly the partial rows that show where it died. The step_event table has no `fallback`
column (§3), so the schema's `fallback` flag is folded into the `detail` JSONB.
"""
from __future__ import annotations

import json

from sqlalchemy import text

_INSERT = text("""
insert into step_event
  (run_result_id, step_index, step_id, intent, action, rung_used, status, error_class,
   budget_ms, elapsed_ms, video_offset_ms, network, detail)
values
  (:rr, :si, :sid, :intent, :action, :rung, :status, :ec,
   :bud, :el, :vo, cast(:net as jsonb), cast(:detail as jsonb))
""")


class DBStepEventSink:
    """Implements ports.StepEventSink. `emit` inserts a row per completed step; `emit_started`
    (B2, optional protocol extension) inserts the live 'running' row at step START."""

    def __init__(self, engine):
        self.engine = engine

    def emit_started(self, run_result_id: str, event: dict) -> None:
        """B2 live grain: TWO ROWS per step — 'running' at start, terminal at completion. Never an
        in-place update: a mutated row is invisible to the id-cursor poll and the SSE tail. The D33
        completeness gate counts terminal rows only (repositories.count_step_events)."""
        self.emit(run_result_id, {**event, "status": "running", "elapsed_ms": None})

    def emit(self, run_result_id: str, event: dict) -> None:
        detail = dict(event.get("detail") or {})
        detail["fallback"] = event.get("fallback", False)
        with self.engine.begin() as c:
            c.execute(_INSERT, {
                "rr": run_result_id,
                "si": event["step_index"],
                "sid": event["step_id"],
                "intent": event.get("intent"),
                "action": event["action"],
                "rung": event.get("rung_used"),
                "status": event["status"],
                "ec": event.get("error_class"),
                "bud": event.get("budget_ms"),
                "el": event.get("elapsed_ms"),
                "vo": event.get("video_offset_ms"),
                "net": json.dumps(event.get("network") or []),
                "detail": json.dumps(detail),
            })
