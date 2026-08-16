"""The step-interpreter — three temperatures (D31, §6). One class; temperature fixed at construction.

`.cold(...)` takes NO healer/author (§1.1) — the zero-LLM steady state, a pure function of
(plan, page-state). `.warm`/`.hot` (M2/M3) receive concrete, LLM-backed Healer/Author *injected*
from anneal-heal / anneal-authoring. Because this package never imports anneal-llm, the cold path's
zero-LLM property is enforced by the resolver (D31), not a code review.
"""
from __future__ import annotations

import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from enum import Enum

from anneal_core.errors import (
    AuthExpired,
    ColdModeViolation,
    LocatorAmbiguous,
    LocatorUnresolved,
    NetworkExpectationUnmet,
    PostconditionUnmet,
    classify_exception,
)

from . import actions, network
from .guard import guard
from .resolve import resolve
from .session import is_login_page
from .waits import check_wait


class Temp(Enum):
    COLD = "cold"
    WARM = "warm"
    HOT = "hot"


@dataclass
class RunOutcome:
    verdict: str                     # green | red  (green_with_heals/provisional are warm/hot, M2/M3)
    error_class: str | None = None
    failed_step_id: str | None = None
    steps_run: int = 0
    fallback_used: bool = False
    events: list[dict] = field(default_factory=list)
    heals: list[dict] = field(default_factory=list)   # WARM: verified re-resolutions (§6.3) → vN+1


def _is_timeout(exc: BaseException) -> bool:
    return type(exc).__name__ == "TimeoutError"


# ── runtime capture substitution (§ GAP-4) — resolve {{capture.<name>}} in a step just before it runs ──
_CAP = re.compile(r"\{\{capture\.([a-z0-9_]+)\}\}")


def _sub(s, caps):
    if not s or "{{capture." not in s:
        return s
    return _CAP.sub(lambda m: str(caps.get(m.group(1), m.group(0))), s)


def _sub_rung(r, caps):
    return replace(r, value=_sub(r.value, caps), name=_sub(r.name, caps))


def _sub_scope(sc, caps):
    return sc if sc is None else replace(sc, testid=_sub(sc.testid, caps),
                                         role=_sub(sc.role, caps), row_label=_sub(sc.row_label, caps))


def _apply_captures(step, caps):
    """Return the step with every {{capture.<name>}} resolved from the accumulated runtime captures —
    across arg, target rungs/scope, postcondition waits, and the assert. A no-op (identity) when nothing
    has been captured yet or the step names no capture, so a capture-free plan is untouched."""
    if not caps:
        return step
    waits = [replace(w, match=_sub(w.match, caps), equals=_sub(w.equals, caps),
                     rungs=[_sub_rung(r, caps) for r in w.rungs], scope=_sub_scope(w.scope, caps))
             for w in step.waits]
    a = step.assert_
    a = a if a is None else replace(a, equals=_sub(a.equals, caps),
                                    rungs=[_sub_rung(r, caps) for r in a.rungs],
                                    scope=_sub_scope(a.scope, caps))
    return replace(step, arg=_sub(step.arg, caps), rungs=[_sub_rung(r, caps) for r in step.rungs],
                   scope=_sub_scope(step.scope, caps), waits=waits, assert_=a)


async def _created_id(resp):
    """The id a create response returned, if the body is JSON carrying one (§10.2 sweeper primary path).
    Best-effort + guarded — a non-JSON or bodyless response just yields None."""
    try:
        body = await resp.json()
    except Exception:
        return None
    if isinstance(body, dict):
        for k in ("id", "_id", "uuid", "key"):
            if body.get(k) is not None:
                return body[k]
    return None


class Interpreter:
    def __init__(self, temperature, page, plan, sink, artifacts, *, token="", run_result_id="run",
                 healer=None, author=None, video_epoch=None):
        self._temperature = temperature
        self.page = page
        self.plan = plan
        self.sink = sink
        self.artifacts = artifacts
        self.token = token
        self.run_result_id = run_result_id
        self._healer = healer
        self._author = author
        self._video_epoch = video_epoch     # Evidence.video_epoch (recording paths); None ⇒ offsets null
        self._any_fb = False
        self._heals: list[dict] = []
        self._captures: dict[str, str] = {}     # § GAP-4: runtime {{capture.*}} values, accumulated in order

    # ── constructors (the temperature is the API, §6) ────────────────────────
    @classmethod
    def cold(cls, page, plan, sink, artifacts, *, token="", run_result_id="run", video_epoch=None):
        return cls(Temp.COLD, page, plan, sink, artifacts, token=token, run_result_id=run_result_id,
                   healer=None, author=None, video_epoch=video_epoch)

    @classmethod
    def warm(cls, page, plan, sink, artifacts, *, healer, token="", run_result_id="run",
             video_epoch=None):
        return cls(Temp.WARM, page, plan, sink, artifacts, token=token, run_result_id=run_result_id,
                   healer=healer, author=None, video_epoch=video_epoch)

    @classmethod
    def hot(cls, page, plan, sink, artifacts, *, author, token="", run_result_id="run",
            video_epoch=None):
        return cls(Temp.HOT, page, plan, sink, artifacts, token=token, run_result_id=run_result_id,
                   healer=None, author=author, video_epoch=video_epoch)

    def _require_llm(self, why: str):
        if self._temperature is Temp.COLD or self._healer is None:
            raise ColdModeViolation(why)          # → ENGINE_FAULT, hard red + incident (§7)
        return self._healer

    # ── fallback telemetry (§4): resolved below the top rung available for this step ─
    @staticmethod
    def _fb(step, rung_used) -> bool:
        return bool(step.rungs) and rung_used is not None and rung_used != step.rungs[0].kind

    @staticmethod
    def _fb_assert(step, rung_used) -> bool:
        rungs = step.assert_.rungs if step.assert_ else []
        return bool(rungs) and rung_used is not None and rung_used != rungs[0].kind

    def _video_offset_ms(self) -> int | None:
        """ms from video start (context creation, Evidence.video_epoch) to now — the scrub position
        this event corresponds to. None on non-recording paths (calibration, collector doubles).
        getattr, not attribute access: test doubles build the interpreter via object.__new__."""
        epoch = getattr(self, "_video_epoch", None)
        if epoch is None:
            return None
        return round((time.monotonic() - epoch) * 1000)

    def _emit_started(self, step):
        """B2 live grain: a status='running' row at step START, for sinks that persist live progress
        (DBStepEventSink → the console's 'step 3 running for 2s' + budget bar). OPTIONAL by duck-type:
        collector doubles (calibration/reauthor/tests) don't implement it, so completed-event streams
        keep exactly one event per step and the D33 collector semantics are untouched."""
        started = getattr(self.sink, "emit_started", None)
        if started is not None:
            event = {"step_index": step.index, "step_id": step.step_id, "intent": step.intent,
                     "action": step.action, "budget_ms": step.budget_ms}
            off = self._video_offset_ms()
            if off is not None:                 # key present exactly when the run is recorded
                event["video_offset_ms"] = off
            started(self.run_result_id, event)

    def _emit(self, step, status, rung_used, fallback, elapsed_ms, *, error_class=None,
              network=None, detail=None):
        if fallback:
            self._any_fb = True
        event = {
            "step_index": step.index,
            "step_id": step.step_id,
            "intent": step.intent,
            "action": step.action,
            "rung_used": rung_used,
            "fallback": bool(fallback),
            "status": status,
            "error_class": error_class,
            "budget_ms": step.budget_ms,
            "elapsed_ms": elapsed_ms,
            "network": network or [],
            "detail": detail or {},
        }
        off = self._video_offset_ms()
        if off is not None:                     # key present exactly when the run is recorded
            event["video_offset_ms"] = off
        self.sink.emit(self.run_result_id, event)
        return event

    async def _resolve_or_heal(self, step):
        """COLD/HOT: plain resolve. WARM (§6.3): on a LOCATOR_DRIFT, call the injected Healer (ladder-
        first, LLM-over-DOM only if the ladder is dead), retry with the healed rungs, and record a
        verified target change so the classify layer can apply vN+1. Returns (locator, rung, healed)."""
        try:
            loc, rung = await resolve(self.page, step.rungs, step.budget_ms, step.scope)
            return loc, rung, False
        except (LocatorUnresolved, LocatorAmbiguous):
            # A5: a step that can't resolve because the app bounced to a sign-in wall is an EXPIRED
            # session, not locator drift. Surface AUTH_EXPIRED (→ park) rather than a product red, and
            # never let the healer re-observe / re-freeze a locator against the login page.
            if await is_login_page(self.page):
                raise AuthExpired(f"{step.step_id}: sign-in page where an authenticated page was expected")
            if self._temperature is not Temp.WARM or self._healer is None:
                raise
            outcome = await self._healer.heal(step, self.page)
            if outcome is None:
                raise                                    # heal failed → the red stands (§7 LOCATOR_DRIFT)
            loc, rung = await resolve(self.page, outcome.rungs, step.budget_ms, step.scope)
            if outcome.target is not None:               # a persistable re-resolution (not a transient retry)
                self._heals.append({
                    "step_id": step.step_id, "step_index": step.index, "intent": step.intent,
                    "action": step.action, "target": outcome.target, "summary": outcome.summary,
                    "old_primary": outcome.old_primary, "new_primary": outcome.new_primary})
                return loc, rung, True
            return loc, rung, False

    async def _do_action(self, step, locator):
        await actions.dispatch(step.action)(self.page, step, locator)

    async def _do_capture(self, step):
        """§ GAP-4: after a step succeeds, extract its declared capture into a run-scoped var for a later
        {{capture.<name>}}. `url` reads page.url (e.g. the id a create navigated to); `text` reads this
        step's resolved target. A pattern that does not match is a real failure — the value a later step
        needs was not produced — so it reds (PostconditionUnmet), never a silent empty."""
        cap = step.capture
        if cap is None:
            return
        if cap.source == "text":
            loc, _ = await resolve(self.page, step.rungs, step.budget_ms, step.scope)
            src = (await loc.inner_text()) or ""
        else:
            src = self.page.url
        m = re.search(cap.pattern, src)
        if not m:
            raise PostconditionUnmet(
                f"capture {cap.name!r}: pattern {cap.pattern!r} did not match the {cap.source}")
        self._captures[cap.name] = m.group(1) if m.groups() else m.group(0)

    async def _act_with_network(self, step, locator):
        """Arm each NetExpectation BEFORE the action (CR-3); assert the matches after."""
        if not step.arm_before:
            await self._do_action(step, locator)
            return []
        infos = []
        try:
            async with AsyncExitStack() as stack:
                for exp in step.arm_before:
                    info = await stack.enter_async_context(
                        self.page.expect_response(network.predicate(exp), timeout=exp.budget_ms))
                    infos.append((exp, info))
                await self._do_action(step, locator)
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise NetworkExpectationUnmet(str(e)) from e
            raise
        out = []
        for exp, info in infos:
            resp = await info.value
            ev = {"method": exp.method, "url": resp.url, "status": resp.status, "matched_expectation": True}
            if exp.method in ("POST", "PUT") and 200 <= resp.status < 300:
                cid = await _created_id(resp)     # record the id a create returned → sweeper's primary path
                if cid is not None:
                    ev["created_id"] = str(cid)
            out.append(ev)
        return out

    # ── the cold loop (the steady state — zero LLM, HLD §3) ───────────────────
    async def run(self) -> RunOutcome:
        for step in self.plan.steps:
            step = _apply_captures(step, self._captures)   # § GAP-4: bind {{capture.*}} from prior steps
            start = time.monotonic()
            self._emit_started(step)                     # B2: live 'running' row (DB sinks only)
            phase = "locate"
            rung_used = None
            try:
                if step.action == "assert":                                     # standalone predicate step (§2.5)
                    phase = "verify"
                    ok, rung_used = await actions.assert_(
                        self.page, step.assert_, step.budget_ms, reload_on_miss=step.reload_on_miss)
                    elapsed = int((time.monotonic() - start) * 1000)
                    fb = self._fb_assert(step, rung_used)
                    if ok:
                        await self._do_capture(step)      # § GAP-4 (a red here if the pattern misses)
                        self._emit(step, "passed", rung_used, fb, elapsed,
                                   detail={"assert": step.assert_.kind})
                        continue
                    ec = classify_exception(PostconditionUnmet("assert oracle returned false"), "verify")
                    self._emit(step, "failed", rung_used, fb, elapsed, error_class=ec.value,
                               detail={"assert": "false"})
                    return RunOutcome("red", ec.value, step.step_id, step.index + 1, self._any_fb,
                                      heals=self._heals)

                locator = None
                healed = False
                if step.action != "goto":
                    locator, rung_used, healed = await self._resolve_or_heal(step)
                    if step.destructive:
                        phase = "act"
                        await guard(locator, self.token)
                phase = "act"
                net = await self._act_with_network(step, locator)
                phase = "verify"
                for w in step.waits:
                    await check_wait(self.page, w)
                await self._do_capture(step)              # § GAP-4 (a red here if the pattern misses)
                elapsed = int((time.monotonic() - start) * 1000)
                fb = False if healed else self._fb(step, rung_used)   # a heal is not a fallback pass
                self._emit(step, "passed", rung_used, fb, elapsed, network=net,
                           detail={"waits": len(step.waits), "healed": healed})
            except Exception as exc:  # noqa: BLE001
                elapsed = int((time.monotonic() - start) * 1000)
                ec = classify_exception(exc, phase)
                self._emit(step, "failed", rung_used, False, elapsed, error_class=ec.value,
                           detail={"error": str(exc)[:300], "phase": phase})
                return RunOutcome("red", ec.value, step.step_id, step.index + 1, self._any_fb,
                                  heals=self._heals)

        return RunOutcome("green", None, None, len(self.plan.steps), self._any_fb, heals=self._heals)
