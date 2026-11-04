"""Behavior re-author (§6.3 case 3, §9.3-9.4). The representative re-authors BLIND (its live walk of
the drifted app yields the provisional version AND the change brief); members re-author GUIDED by the
brief. Every re-author is budget-guarded (3.1b) and applied immediately as PROVISIONAL (the suite goes
green now — §6), then VERIFIED by a cold replay against the still-drifted app. A re-author that cannot
turn green means the agent could not complete the intent → PRODUCT_BUG, not a provisional feature.
"""
from __future__ import annotations

import json
from datetime import date

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from anneal_authoring.author import AuthoringIncomplete, author_bundle
from anneal_authoring.calibrate import CalibrationError, calibrate
from anneal_core.bundle import Bundle
from anneal_core.compile import compile as compile_bundle
from anneal_core.errors import (CompileError, EngineFault, LocatorAmbiguous, LocatorUnresolved,
                                 PlaceholderError, UnsupportedVerb)
from anneal_core.stepplan import CompileContext
from anneal_db import repositories as repo
from anneal_db.engine import mint_token
from anneal_executor.evidence import recording
from anneal_executor.interpreter import Interpreter

from . import incidents

# EXPECTED LLM-authoring failures for one re-author walk: a mis-picked/rotted locator, a browser action
# that timed out, a bundle that won't compile, or a non-falsifiable oracle. These are honest walk failures
# (the agent couldn't author a working flow on the live app), handled by retry/degrade. Engine faults —
# a test_version FK race, a None-subscript bug — are DELIBERATELY excluded: they must stay loud and be
# fixed from their traceback, never swallowed into a false product_bug (see anneal-boundary-scoping-rule).
_WALK_FAILURE = (LocatorUnresolved, LocatorAmbiguous, CompileError, CalibrationError,
                 AuthoringIncomplete, PlaywrightError, PlaywrightTimeout, json.JSONDecodeError)


def _fill_labels(bundle: dict) -> set[str]:
    out = set()
    for s in bundle.get("steps", []):
        if s.get("action") in ("fill", "select"):
            t = s.get("target") or {}
            label = (t.get("fallbacks") or {}).get("text") or t.get("selector") or s.get("intent")
            if label:
                out.add(str(label).strip().lower())
    return out


def derive_change_brief(old_bundle: dict, new_bundle: dict) -> str:
    """A compact NL statement of what the app now does differently — deterministic from the step diff,
    always naming what the agent had to do that the old steps didn't (so a bypass is visible, §9.3)."""
    added = _fill_labels(new_bundle) - _fill_labels(old_bundle)
    n_old, n_new = len(old_bundle.get("steps", [])), len(new_bundle.get("steps", []))
    bits = []
    if added:
        bits.append("now requires filling: " + ", ".join(sorted(added)))
    if n_new != n_old:
        bits.append(f"flow length {n_old}→{n_new} steps")
    return "; ".join(bits) or "create/edit flow changed shape (re-authored against the live app)"


async def _verify_green(engine, artifacts, target, bundle: dict, modules, *, red_run_id) -> tuple[bool, str]:
    """B3: the verify replay is a REAL run_result (role='verify', child of the red run) — its OWN
    artifact dir (the old fixed 'reauthor-verify' dir was overwritten by every incident) and a real
    step trail, so the money-shot can show the verify evidence beside the red. Returns
    (verified, verify_run_id)."""
    import time as _time

    from anneal_db.sink import DBStepEventSink
    tok = mint_token()
    ctxc = CompileContext(token=tok, today=date.today(), date_format=target.get("date_format") or "%Y-%m-%d",
                          params={}, modules=modules)
    plan = compile_bundle(Bundle.model_validate(bundle), ctxc)
    storage_state = repo.get_storage_state(engine, project_id=target["project_id"], persona=bundle["persona"],
                                           target_id=target["id"])    # THIS platform's session (§3.5)
    verify_id = repo.start_child_run(engine, parent_run_id=red_run_id, role="verify")
    started = _time.monotonic()
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        br = await p.chromium.launch()
        try:
            async with recording(br, artifacts, verify_id, base_url=target["base_url"],
                                 storage_state=storage_state) as (page, ev):
                outcome = await Interpreter.cold(page, plan, DBStepEventSink(engine), artifacts,
                                                 token=tok, run_result_id=verify_id,
                                                 video_epoch=ev.video_epoch).run()
        finally:
            await br.close()
    repo.finish_child_run(engine, run_result_id=verify_id, verdict=outcome.verdict,
                          error_class=outcome.error_class, trace_path=ev.trace_path,
                          video_path=ev.video_path, screenshot_path=ev.screenshot_path,
                          network_log_path=ev.network_log_path,
                          duration_ms=int((_time.monotonic() - started) * 1000))
    if outcome.error_class == "ENGINE_FAULT":
        # an engine fault during the verify replay is OUR bug, not "the re-author didn't verify". Re-surface
        # it LOUD (→ scheduler ENGINE_FAULT) instead of collapsing to False, which would launder an engine
        # bug into a false product_bug — the same masking hole the walk-boundary closes (boundary-scoping).
        raise EngineFault(f"engine fault during re-author verify replay (step {outcome.failed_step_id})")
    return outcome.verdict in ("green", "green_with_heals"), verify_id


async def reauthor_member(engine, artifacts, *, project_id, target, test_id, nl, incident_id, modules,
                          red_run_id, change_brief=None, persona="member", model=None) -> dict | None:
    """Re-author one member against the live (drifted) app. Returns None if the budget is exhausted
    (partial results at the inbox — never a silent retry storm, D27).

    Up to three walks, each budget-guarded (3.1b): a behavior re-author knows the app changed (its
    frozen version red'd a postcondition, §7), so every walk carries honest general guidance; a walk
    that does not VERIFY green gets escalating 'fill every required field' emphasis before we conclude
    the agent cannot complete the intent (PRODUCT_BUG). A provisional version is created ONLY once a
    walk verifies — a failed walk applies nothing (the head stays on the parent), so a genuine product
    bug never ships as a feature."""
    t = repo.get_test(engine, test_id=test_id)
    test_key, head = t["test_key"], t["head_version"]
    guided = bool(change_brief)                          # a member re-authored WITH the rep's brief (§9.4)
    old_bundle = repo.load_bundle(engine, test_id=test_id, version=head)
    start_path = (target.get("profile") or {}).get("probe_path") or "/"
    storage_state = repo.get_storage_state(engine, project_id=project_id, persona=persona,
                                           target_id=target["id"])    # THIS platform's session (§3.5)

    # A behavior re-author KNOWS (deterministically — the frozen version red'd a postcondition, §7) that
    # the app changed under this flow. That is honest general guidance, not the answer: walk it as it is
    # NOW and be thorough with newly-required fields/steps. change_brief (guided members) adds specifics.
    guidance = ("This flow's frozen version just failed because the app changed. Walk the app AS IT IS "
                "NOW and complete the SAME intent: fill EVERY field the form marks required (watch for "
                "'*', 'required', or 'is required' hints) and follow any new required steps before "
                "submitting or confirming.")
    hint = change_brief
    last_bundle = last_calib = None
    verified = False
    verify_run_id = None
    attempt = 0
    while True:                                          # D27 BUDGET is the sole bound (not a fixed 3):
        if not incidents.spend_agent_run(engine, incident_id):
            break                                        # budget exhausted → surface partial (§9, D27)
        # A behavior re-author is a STOCHASTIC authoring step (the scaffold plans from recon, where a
        # form's newly-required field is not yet visible, so a walk occasionally omits it → 422 → red).
        # Retrying-until-verified within the budget is how a nondeterministic author yields a
        # DETERMINISTIC OUTCOME — never relaxing: every retry still must VERIFY green before a provisional
        # version is created; the budget (not a fixed count) caps the spend so there is no retry storm.
        emphasis = "" if attempt == 0 else "\n\nThe last attempt still did not succeed — be exhaustive: fill every required field."
        full_nl = f"{nl}\n\n{guidance}" + (f"\n\nKnown recent change: {hint}" if hint else "") + emphasis
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    ctx = await browser.new_context(base_url=target["base_url"], storage_state=storage_state,
                                                    viewport={"width": 1280, "height": 720})
                    page = await ctx.new_page()
                    result = await author_bundle(page, nl=full_nl, project_id=project_id, test_id=test_key,
                                                 persona=persona, target_key=target["key"],
                                                 start_path=start_path, model=model)
                    await ctx.close()
                    bundle = result.bundle
                    bundle["state"] = "frozen"           # a frozen+calibrated artifact; provisional is DB state
                    calib = await calibrate(browser, target["base_url"], bundle, start_path,
                                            storage_state=storage_state)
                finally:
                    await browser.close()
            bundle["calibration"] = calib
            last_bundle, last_calib = bundle, calib
            verified, verify_run_id = await _verify_green(engine, artifacts, target, bundle, modules,
                                                          red_run_id=red_run_id)   # ephemeral — no version yet
        except (UnsupportedVerb, PlaceholderError):
            # D16/D36 ENGINE GAPS masquerading as CompileError — the compiler can't express the verb /
            # a placeholder is unresolved. These are the engine's fault, not a walk failure: re-raise so
            # they stay LOUD (→ ENGINE_FAULT), never mislabelled a product_bug (boundary-scoping rule).
            raise
        except _WALK_FAILURE as exc:
            # This walk could not author a working flow on the live app (expected LLM-authoring failure) —
            # an honest failed ATTEMPT: record it, escalate, and retry. Engine faults are NOT caught here
            # (not in _WALK_FAILURE) so they stay loud — never a false product_bug (boundary-scoping rule).
            repo.changelog_add(engine, project_id=project_id, kind="reauthor_walk_failed",
                               summary=f"{test_key}: re-author walk {attempt + 1} could not author a working "
                                       f"flow ({type(exc).__name__}: {str(exc)[:160]}) — retrying",
                               test_id=test_id, incident_id=incident_id)
            verified = False
        if verified:
            break
        # the walk didn't complete the intent — try again, this time naming the obstacle explicitly.
        hint = ((change_brief + "; ") if change_brief else "") + \
            "the form has NEW required fields (marked required/*) — fill EVERY required field before submitting"
        attempt += 1

    if last_bundle is None:
        return None                                      # budget exhausted before any walk

    brief = change_brief or derive_change_brief(old_bundle, last_bundle)
    if verified:
        reason = "reauthor_guided" if guided else "reauthor"    # guided = consumed the rep's brief
        new_v = repo.create_test_version(engine, test_id=test_id, bundle=last_bundle, reason=reason,
                                         parent_version=head, verified_against=target["key"],
                                         state="provisional", incident_id=incident_id,
                                         triggered_by="hot_author", calibration=last_calib, apply=True)
        repo.set_member_applied(engine, incident_id=incident_id, test_id=test_id, applied_version=new_v)
        if verify_run_id:
            # B3: link the verify replay that PROVED this provisional — the money-shot's second half
            repo.set_member_verify_run(engine, incident_id=incident_id, test_id=test_id,
                                       verify_run_id=verify_run_id)
        return {"version": new_v, "verified": True, "change_brief": brief, "test_key": test_key}

    # could not complete the intent → PRODUCT_BUG. Apply nothing; the head stays on the parent (§6.3).
    repo.changelog_add(engine, project_id=project_id, kind="product_bug",
                       summary=f"{test_key}: re-author could not complete the intent on the drifted app",
                       test_id=test_id, incident_id=incident_id)
    return {"version": None, "verified": False, "change_brief": brief, "test_key": test_key}
