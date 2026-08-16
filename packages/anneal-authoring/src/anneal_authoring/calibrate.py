"""Calibration (D7, freeze-time) — proves the frozen oracle is falsifiable, so a green replay MEANS
something (this is the complement of the N1 oracle-honesty gate).

- **negative control (C-1/D7 + D7.1) — flow-type-aware:** each present assert (visible OR value_is)
  is evaluated against a CLEAN state where it MUST fail — an oracle that passes on a state where its
  subject does not exist is rubber-stamping (defined-into-always-passing). What "clean state" means
  depends on what the flow does:
    - MUTATING flow (creates/changes an entity, `policy.mutating`) → **token-swap** (original D7): the
      flow is recompiled with a run token that was never created and its `goto` navigation replayed so
      each assert is checked on the page it targets, on the SAME storage_state as authoring — so a
      present-assert on the (now never-created) item fails for the RIGHT reason (the item's absence),
      not because it landed on a login wall.
    - NON-MUTATING flow (a smoke that creates nothing — its asserts are static, auth-gated content) →
      **auth-swap (D7.1):** the SAME negative control runs on an ANONYMOUS context (storage_state
      dropped). An assert that still passes logged-out is not auth-gated → rubber-stamp (rejected as
      ever); one that fails at the login wall is discriminative → falsifiable → freeze. This is the
      only falsifiability a create-nothing smoke has, and it matches how a login/render smoke is
      meaningful in both parents. Target-agnostic — it just drops the session; no login/app specifics.
      (N1, the oracle-honesty negative, is non-mutating: it takes the auth-swap path, still freezes,
      and still reds on the real authenticated replay.)
- **network discriminativeness (§5.2/§7):** each authored network expectation's (method, templated
  path) must be unique among the flow's mutating steps — a POST-any / DELETE-any that matches every
  call is not discriminative.
- **falsifiable floor (D7):** a bundle with neither a negative-controlled present-assert nor a
  discriminative network expectation has nothing to falsify — a green replay would be meaningless, so
  it is rejected rather than frozen.
"""
from __future__ import annotations

from datetime import date

from anneal_core.bundle import Bundle
from anneal_core.compile import compile as compile_bundle
from anneal_core.errors import LocatorAmbiguous, LocatorUnresolved, PostconditionUnmet
from anneal_core.stepplan import CompileContext
from anneal_db.engine import mint_token
from anneal_executor import actions
from anneal_executor.interpreter import Interpreter
from anneal_executor.settle import await_settled


class CalibrationError(Exception):
    pass


class _NullSink:
    """The positive replay is a GATE, not evidence — drop every step_event (Interpreter only ever calls
    `.emit`, and probes `.emit_started` via getattr, so this is the whole surface)."""

    def emit(self, run_result_id, event):
        return None


def _network_conditions(bundle: dict) -> list[tuple[str, dict]]:
    out = []
    for s in bundle["steps"]:
        for c in ((s.get("postcondition") or {}).get("all") or []):
            if c.get("kind") == "network":
                out.append((s["id"], c))
    return out


def _is_present_assert(ps) -> bool:
    """A falsifiable present-oracle: one that must be UNTRUE on a clean/pre state. element-visible and
    value_is qualify, as do the state-observing kinds that assert a POSITIVE state — checked=true, a
    positive count, an attribute/text equality. An absence-like variant (element 'hidden', checked=false,
    count=0) is trivially true when the subject was never created, so it is NOT negative-controllable and
    is excluded (same reasoning as 'hidden')."""
    a = ps.assert_
    if not (ps.action == "assert" and a):
        return False
    if a.kind == "element":
        return a.state == "visible"
    if a.kind in ("value_is", "attribute", "text"):
        return True
    if a.kind == "checked":
        return a.checked is True
    if a.kind == "count":
        return (a.count or 0) > 0
    return False


async def calibrate(browser, base_url: str, bundle: dict, start_path: str, storage_state=None) -> dict:
    # ── network discriminativeness ──
    net = _network_conditions(bundle)
    sigs = [(c["method"], c["path"]) for _, c in net]
    discriminativeness = {
        sid: ("unique_in_authoring_log" if sigs.count((c["method"], c["path"])) == 1 else "ambiguous")
        for sid, c in net
    }
    bad = [sid for sid, v in discriminativeness.items() if v != "unique_in_authoring_log"]
    if bad:
        raise CalibrationError(f"network expectation(s) not discriminative: {bad}")

    # ── negative control for present-asserts ──
    # compile with a token that was NEVER created → the item cannot exist on a clean state
    plan = compile_bundle(Bundle.model_validate(bundle),
                          CompileContext(token="nevercre8d", today=date.today(),
                                         date_format="%Y-%m-%d", params={}, modules={}))
    negative_control: dict = {}
    present = [ps for ps in plan.steps if _is_present_assert(ps)]
    if present:
        # D7.1: MUTATING → token-swap on the authenticated state (unchanged); NON-MUTATING → AUTH-SWAP,
        # i.e. run the negative control on an ANONYMOUS context so a static, auth-gated smoke is
        # falsified by the login wall rather than rubber-stamping. Branch on the bundle's existing flag.
        mutating = bool((bundle.get("policy") or {}).get("mutating"))
        control_storage_state = storage_state if mutating else None
        ctx = await browser.new_context(base_url=base_url, storage_state=control_storage_state,
                                        viewport={"width": 1280, "height": 720})
        try:
            page = await ctx.new_page()
            malformed: dict[str, str] = {}
            await page.goto(start_path, wait_until="domcontentloaded", timeout=30000)
            await await_settled(page)
            # replay the flow's NAVIGATION (goto steps) so each assert is checked on the page it
            # targets — not blanket-checked on start_path. Acts (click/fill on the never-created item)
            # are skipped; an assert unreachable without them fails-to-resolve, which is the honest
            # verdict (you cannot reach a never-created item's page).
            for ps in plan.steps:
                if ps.action == "goto":
                    try:
                        await page.goto(ps.arg or start_path, wait_until="domcontentloaded", timeout=15000)
                        await await_settled(page)      # assert on the hydrated page, not an empty shell
                    except Exception:
                        pass
                    continue
                if not _is_present_assert(ps):
                    continue
                try:
                    ok, _ = await actions.assert_(page, ps.assert_, 3000, reload_on_miss=False)
                    negative_control[ps.step_id] = "failed_on_pre_state" if not ok else "RUBBER_STAMP"
                except (LocatorUnresolved, LocatorAmbiguous, PostconditionUnmet):
                    negative_control[ps.step_id] = "failed_on_pre_state"   # honest: absent on the clean/pre state
                except Exception as exc:                                   # noqa: BLE001
                    # WELL-FORMED (check #1): a NON-resolution error is not "absent on the pre-state" — it is an
                    # executor-INCOMPATIBLE oracle that would throw on EVERY state, so a green replay proves
                    # nothing (the witnessing bug: a `value_is` whose target resolved to a <div> combobox
                    # trigger, not an input → input_value() raises). The old blanket `except` miscounted that
                    # THROW as "correctly could not find it" and let the malformed oracle freeze. A visibility/
                    # resolve TIMEOUT is still honest absence (the item legitimately isn't there); anything else
                    # means the frozen predicate can never be evaluated → fail the freeze, don't launder it.
                    if type(exc).__name__ == "TimeoutError":               # playwright visibility/resolve timeout
                        negative_control[ps.step_id] = "failed_on_pre_state"
                    else:
                        malformed[ps.step_id] = repr(exc)
        finally:
            await ctx.close()
        if malformed:
            raise CalibrationError(
                "malformed oracle — not executor-evaluable on any state (a value_is target must be an "
                f"input-capable element; it threw on the clean pre-state): {malformed}")
        stamps = [sid for sid, v in negative_control.items() if v != "failed_on_pre_state"]
        if stamps:
            raise CalibrationError(f"negative control failed — oracle rubber-stamps on a clean state: {stamps}")

    # ── falsifiable floor (D7): a bundle with nothing to falsify must not freeze ──
    if not negative_control and not discriminativeness:
        raise CalibrationError("no falsifiable check authored — a green replay would be meaningless (D7): "
                               "the bundle has neither a negative-controlled present-assert nor a "
                               "discriminative network expectation")

    # ── positive-replay gate (check #3): stability ≠ falsifiability ──
    # The negative control proves each oracle is ABSENT-on-the-pre-state (falsifiable). That is necessary
    # but not sufficient: an oracle can be falsifiable yet still (a) resolve AMBIGUOUSLY on the real state
    # (P5: a bare-text rung matching >1 element) or (b) never actually be TRUE on a clean real run (a
    # never-true oracle "fails on the pre-state" too, so the negative-only gate rated it falsifiable). Run
    # the flow ONCE, for real, on the authenticated state and require GREEN — reusing the one cold
    # interpreter (no re-implemented act dispatch): resolve() enforces count()==1 so ambiguity reds, and a
    # false/malformed present-assert or effect-oracle wait reds. A red here means a frozen oracle does not
    # hold on a clean replay → refuse to freeze. (record_golden's post-freeze replay stays as-is: it is
    # best-effort for the video; this is the gate.)
    pos_tok = mint_token()
    pos_plan = compile_bundle(Bundle.model_validate(bundle),
                              CompileContext(token=pos_tok, today=date.today(),
                                             date_format="%Y-%m-%d", params={}, modules={}))
    ctx = await browser.new_context(base_url=base_url, storage_state=storage_state,
                                    viewport={"width": 1280, "height": 720})
    try:
        page = await ctx.new_page()
        # `artifacts` is inert on the cold run path (stored, never read) and a calibration replay captures
        # no evidence, so pass None rather than thread an ArtifactStore through the freeze path.
        outcome = await Interpreter.cold(page, pos_plan, _NullSink(), None,
                                         token=pos_tok, run_result_id="calibrate").run()
    finally:
        await ctx.close()
    if outcome.verdict != "green":
        raise CalibrationError(
            "positive replay not green — a frozen oracle does not hold on a clean authenticated replay "
            f"(step {outcome.failed_step_id}, {outcome.error_class}): a green replay would be meaningless")

    calib = {}
    calib.update({sid: {"negative_control": v} for sid, v in negative_control.items()})
    calib.update({sid: {"network_discriminativeness": v} for sid, v in discriminativeness.items()})
    calib["positive_replay"] = {"verdict": outcome.verdict, "steps_run": outcome.steps_run}
    return calib
