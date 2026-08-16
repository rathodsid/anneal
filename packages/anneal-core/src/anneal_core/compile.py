"""The compiler — Bundle → StepPlan (§5). Pure: DB-free, LLM-free, Playwright-free.

Called once per run at run start; both the interpreter (§6) and `anneal export` (§5.5) consume the
same StepPlan, so an exported script can never skew from what runs. Stages run in the §5.1 order:
verb check → module inline → placeholder resolution → rung derivation → postcondition lowering →
assert lowering → guard flag pass.
"""
from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace

from .bundle import ActionStep, AssertPredicate, Bundle, Postcondition, Target
from .errors import AssertOracleError, CompileError, PlaceholderError, UnsupportedVerb
from .rungs import derive_rungs
from .stepplan import (
    AssertSpec,
    Capture,
    CompileContext,
    NetExpectation,
    PlanStep,
    Scope,
    StepPlan,
    Wait,
)

_VERBS = {"goto", "click", "fill", "select", "upload", "keypress", "hover", "assert"}

# ── placeholder registry (§2.4, D36) — the ONLY legal token grammar ──────────
_TOKEN_SPAN = re.compile(r"\{\{(.*?)\}\}")
_TODAY_DELTA = re.compile(r"run\.today([+-])(\d{1,3})d")
_PARAM = re.compile(r"param\.([a-z0-9_]+)")
_CAPTURE = re.compile(r"capture\.([a-z0-9_]+)")     # runtime-resolved (§ GAP-4) — preserved at compile


def _resolve_one(name: str, ctx: CompileContext) -> str:
    if name == "run.token":
        return ctx.token
    if name == "run.today":
        return ctx.today.strftime(ctx.date_format)
    m = _TODAY_DELTA.fullmatch(name)
    if m:
        n = int(m.group(2))
        delta = timedelta(days=n if m.group(1) == "+" else -n)
        return (ctx.today + delta).strftime(ctx.date_format)
    m = _PARAM.fullmatch(name)
    if m:
        key = m.group(1)
        if key not in ctx.params:
            raise PlaceholderError(f"unknown param {{{{param.{key}}}}} — not in params row")
        return ctx.params[key]
    if _CAPTURE.fullmatch(name):
        return "{{" + name + "}}"      # runtime capture (§ GAP-4): preserve verbatim, resolved at step time
    raise PlaceholderError(f"token {{{{{name}}}}} is outside the closed registry (D36)")


def resolve_placeholders(text: str, ctx: CompileContext) -> str:
    """Substitute every {{…}} token in `text`. Single-brace `{id}` (a network wildcard) is left
    intact — only the double-brace grammar is a value placeholder (§2.4)."""
    return _TOKEN_SPAN.sub(lambda mo: _resolve_one(mo.group(1).strip(), ctx), text)


def _rp(v: str | None, ctx: CompileContext) -> str | None:
    return resolve_placeholders(v, ctx) if v else v


def _resolve_deep(obj, ctx: CompileContext):
    """Resolve {{…}} tokens anywhere inside a nested structure (§5.1 stage 3 covers network `response`)."""
    if isinstance(obj, str):
        return resolve_placeholders(obj, ctx)
    if isinstance(obj, dict):
        return {k: _resolve_deep(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_deep(v, ctx) for v in obj]
    return obj


# ── network path templating (§5.2): `{id}` → `[^/]+`, literal parts escaped ───
_BRACE1 = re.compile(r"\{[a-z0-9_]+\}")


def _path_to_regex(path: str) -> str:
    out: list[str] = []
    for part in re.split(r"(\{[a-z0-9_]+\})", path):
        out.append("[^/]+" if _BRACE1.fullmatch(part) else re.escape(part))
    return "".join(out)


def _resolve_target(t: Target, ctx: CompileContext) -> SimpleNamespace:
    fb = None
    if t.fallbacks:
        fb = SimpleNamespace(
            testid=_rp(t.fallbacks.testid, ctx),
            role=_rp(t.fallbacks.role, ctx),
            text=_rp(t.fallbacks.text, ctx),
            placeholder=_rp(t.fallbacks.placeholder, ctx),
            css=_rp(t.fallbacks.css, ctx),
        )
    return SimpleNamespace(strategy=t.strategy, selector=resolve_placeholders(t.selector, ctx), fallbacks=fb)


def _to_scope(t: Target | None, ctx: CompileContext) -> Scope | None:
    if t is None or t.within is None:
        return None
    w = t.within
    return Scope(testid=w.testid, role=w.role,
                 row_label=_rp(w.row_label, ctx))


def _lower_postcondition(pc: Postcondition, ctx: CompileContext) -> tuple[list[Wait], list[NetExpectation]]:
    waits: list[Wait] = []
    arm: list[NetExpectation] = []
    for pred in pc.all:
        if pred.kind == "url":
            waits.append(Wait(kind="url", match=resolve_placeholders(pred.match, ctx), budget_ms=pc.budget_ms))
        elif pred.kind == "element":
            rt = _resolve_target(pred.target, ctx)
            waits.append(Wait(kind="element", state=pred.state, rungs=derive_rungs(rt),
                              scope=_to_scope(pred.target, ctx), budget_ms=pc.budget_ms))
        elif pred.kind == "value_is":
            rt = _resolve_target(pred.target, ctx)
            waits.append(Wait(kind="value_is", equals=resolve_placeholders(pred.equals, ctx),
                              rungs=derive_rungs(rt), scope=_to_scope(pred.target, ctx), budget_ms=pc.budget_ms))
        elif pred.kind == "network":
            arm.append(NetExpectation(
                method=pred.method,
                path_pattern=_path_to_regex(resolve_placeholders(pred.path, ctx)),
                status_class=pred.status, budget_ms=pc.budget_ms,
                response_predicate=_resolve_deep(pred.response, ctx) if pred.response else None))
        elif pred.kind in ("checked", "attribute", "text", "count"):
            waits.append(_lower_state_pred(pred, _resolve_target(pred.target, ctx),
                                           _to_scope(pred.target, ctx), pc.budget_ms, ctx))
    return waits, arm


def _lower_state_pred(pred, rt, scope, budget_ms, ctx) -> Wait:
    """Lower a state-observing predicate (checked/attribute/text/count) to a Wait/AssertSpec-shared
    shape. `count` resolves its OWN match tally (not the count()==1 resolver), so it keeps rungs but the
    executor counts matches; the others resolve a single element and read a property. Target-agnostic."""
    rungs = derive_rungs(rt)
    if pred.kind == "checked":
        return Wait(kind="checked", checked=pred.checked, rungs=rungs, scope=scope, budget_ms=budget_ms)
    if pred.kind == "attribute":
        return Wait(kind="attribute", attr_name=pred.name, equals=resolve_placeholders(pred.equals, ctx),
                    rungs=rungs, scope=scope, budget_ms=budget_ms)
    if pred.kind == "text":
        return Wait(kind="text", equals=resolve_placeholders(pred.equals, ctx),
                    contains=bool(pred.contains), rungs=rungs, scope=scope, budget_ms=budget_ms)
    return Wait(kind="count", count=pred.count, rungs=rungs, scope=scope, budget_ms=budget_ms)


def _lower_assert(ap: AssertPredicate, ctx: CompileContext) -> AssertSpec:
    rt = _resolve_target(ap.target, ctx)
    rungs = derive_rungs(rt)
    scope = _to_scope(ap.target, ctx)
    if ap.kind is not None:
        # a state-observing oracle (checked/attribute/text/count) — lower via the shared shape so the
        # standalone-assert and postcondition paths evaluate identically (one executor helper).
        w = _lower_state_pred(ap, rt, scope, 0, ctx)
        return AssertSpec(kind=w.kind, rungs=rungs, scope=scope, equals=w.equals,
                          attr_name=w.attr_name, contains=w.contains, count=w.count, checked=w.checked)
    if ap.state is not None:
        # CR-4: `negative` must never stack on a visible/hidden state predicate (double negation).
        if ap.negative:
            raise AssertOracleError("negative:true on a state predicate inverts the oracle (CR-4)")
        return AssertSpec(kind="element", state=ap.state, negative=False, rungs=rungs, scope=scope)
    if ap.equals is not None:
        return AssertSpec(kind="value_is", equals=resolve_placeholders(ap.equals, ctx),
                          negative=bool(ap.negative), rungs=rungs, scope=scope)
    raise AssertOracleError("assert predicate has neither `state` nor `equals`")


def _inline_modules(steps: list[dict], ctx: CompileContext) -> list[tuple[str, dict]]:
    """Replace each module ref with its resolved steps; prefix ids with `ref/` so the step_id (and
    thus §9 signature) derives from (module ref + intra-module id), never inlined position (§5.1)."""
    out: list[tuple[str, dict]] = []
    for raw in steps:
        if "module" in raw:
            pin = raw["module"]
            key = (pin["ref"], pin["version"])
            mod = ctx.modules.get(key)
            if mod is None:
                raise CompileError(f"unresolved module {pin['ref']}@{pin['version']}")
            for s in mod:
                out.append((f"{pin['ref']}/{s['id']}", s))
        else:
            out.append((raw["id"], raw))
    return out


def _lower_step(step_id: str, index: int, step: ActionStep, ctx: CompileContext) -> PlanStep:
    arg = resolve_placeholders(step.arg, ctx) if step.arg is not None else None
    if step.action in ("goto", "assert"):
        rungs = []                                  # goto has no target; assert's rungs live in AssertSpec
    else:
        rungs = derive_rungs(_resolve_target(step.target, ctx))
    scope = _to_scope(step.target, ctx)
    waits, arm = _lower_postcondition(step.postcondition, ctx) if step.postcondition else ([], [])
    assert_spec = _lower_assert(step.assert_, ctx) if step.action == "assert" else None
    budget = step.budget_ms or (step.postcondition.budget_ms if step.postcondition else None) or 10000
    capture = (Capture(name=step.capture.name, source=step.capture.from_, pattern=step.capture.pattern)
               if step.capture else None)
    return PlanStep(
        step_id=step_id, index=index, intent=step.intent, action=step.action, arg=arg,
        rungs=rungs, scope=scope, waits=waits, arm_before=arm, assert_=assert_spec,
        destructive=bool(step.destructive), reload_on_miss=bool(step.reload_on_miss), budget_ms=budget,
        capture=capture)


def _step_capture_refs(ps: PlanStep) -> set[str]:
    """Every {{capture.<name>}} referenced by a lowered step — across arg, rung values/names, scope,
    postcondition waits, and the assert — so the closed-registry discipline (D36) extends to captures:
    a reference with no earlier producing capture is a compile error, never a silent empty at runtime."""
    refs: set[str] = set()

    def scan(s):
        if s:
            refs.update(_CAPTURE.fullmatch(t).group(1) for t in _TOKEN_SPAN.findall(s) if _CAPTURE.fullmatch(t))

    def scan_rungs(rungs):
        for r in rungs or []:
            scan(r.value); scan(r.name)

    def scan_scope(sc):
        if sc:
            scan(sc.testid); scan(sc.role); scan(sc.row_label)

    scan(ps.arg)
    scan_rungs(ps.rungs)
    scan_scope(ps.scope)
    for w in ps.waits:
        scan(w.match); scan(w.equals); scan_rungs(w.rungs); scan_scope(w.scope)
    if ps.assert_:
        scan(ps.assert_.equals); scan_rungs(ps.assert_.rungs); scan_scope(ps.assert_.scope)
    return refs


def compile(bundle: Bundle, ctx: CompileContext) -> StepPlan:
    plan_steps: list[PlanStep] = []
    for index, (step_id, raw) in enumerate(_inline_modules(bundle.steps, ctx)):
        step = ActionStep.model_validate(raw)
        if step.action not in _VERBS:               # CR-1
            raise UnsupportedVerb(step.action)
        plan_steps.append(_lower_step(step_id, index, step, ctx))
    # capture must be PRODUCED before it is REFERENCED (defined-before-use) — a forward/unknown
    # {{capture.x}} can never resolve at runtime, so reject it at compile (closed-registry honesty, D36).
    produced: set[str] = set()
    for ps in plan_steps:
        missing = _step_capture_refs(ps) - produced
        if missing:
            raise PlaceholderError(
                f"step {ps.step_id!r} references capture(s) {sorted(missing)} not produced by an earlier "
                f"step — a {{{{capture.x}}}} must be captured before it is used")
        if ps.capture:
            produced.add(ps.capture.name)
    return StepPlan(bundle_ref=(bundle.test_id, bundle.version), steps=plan_steps)
