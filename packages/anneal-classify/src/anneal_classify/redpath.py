"""Red-path orchestration (§6/§9) — the glue the scheduler calls after a cold run.

A GREEN run that leaned on a fallback rung → deterministic re-freeze (promote the rung, §6.1).
A repeatable RED run routes on its error_class (A12): LOCATOR_DRIFT → warm heal (fresh run, never
attach to the dead browser — §6.3); POSTCONDITION/NETWORK unmet → behavior re-author → provisional
(§9); env/auth → park; policy/engine → stop. A heal/behavior failure recurring on the same step
signature trips the oscillation stop (D27): quarantine + incident, spend nothing.
"""
from __future__ import annotations

import json

from anneal_core.signatures import step_signature
from anneal_db import repositories as repo
from anneal_executor.evidence import recording
from anneal_executor.interpreter import Interpreter
from anneal_heal import LadderHealer

from . import incidents
from .reauthor import reauthor_member
from .refreeze import refreeze_fallbacks
from .router import Lane, route


def _sig_for(plan, failed_step_id) -> str:
    step = next((s for s in plan.steps if s.step_id == failed_step_id), None)
    return step_signature(step) if step else f"-|-|{failed_step_id}||"


async def _await_brief(engine, incident_id, ceiling_s=600) -> str | None:
    """A guided member waits for the discoverer's change_brief (§9.4), EVENT-DRIVEN: it returns the brief
    the moment it lands, and returns None the moment the discoverer has CONCLUDED without one
    (`rep_concluded`, set on every discoverer exit path incl. a crash) so the member proceeds blind
    (re-election lets it publish the brief itself if it wins). This preserves "1 discovery + N guided"
    under real parallelism (the waiter gets the brief when the discoverer publishes) WITHOUT ever stalling
    on a brief that will never come. `ceiling_s` is a generous BACKSTOP only — it covers a real author
    walk and only fires if the concluded-signal is somehow missed. Under a serial run the discoverer has
    already concluded before this is called, so it returns on the first poll."""
    import asyncio
    for _ in range(max(1, ceiling_s)):
        inc = repo.get_incident(engine, incident_id=incident_id)
        if inc and inc.get("change_brief"):
            return inc["change_brief"]
        if inc and inc.get("rep_concluded"):
            return None                               # discoverer done, no brief → proceed blind (re-elect)
        await asyncio.sleep(1)
    return None


def _nl_of(bundle: dict) -> str:
    for src in (bundle.get("intent") or {}).get("sources", []):
        if src.get("kind") == "nl" and src.get("verbatim"):
            return src["verbatim"]
    return (bundle.get("intent") or {}).get("descriptor") or ""


def on_green_run(engine, *, project_id, target, test_id, run_result_id, plan, bundle) -> str:
    """Green cold run. If it passed any step on a fallback rung, auto re-freeze (§6.1). Returns the
    D29 run verdict: green_with_heals when a fallback was promoted, else green."""
    promotions = refreeze_fallbacks(engine, project_id=project_id, target=target, test_id=test_id,
                                    run_result_id=run_result_id, plan=plan, bundle=bundle)
    return "green_with_heals" if promotions else "green"


async def _heal_and_apply(engine, artifacts, *, project_id, target, test_id, red_run_id, plan,
                          token, persona) -> bool:
    """A fresh WARM run heals the drifted locators (§6.3); verified heals become vN+1 + a closed
    locator incident per heal (heal-as-changelog, §8). Returns True iff the warm run turned green.
    B3: the warm run is a REAL run_result (role='heal') — own artifact dir + step trail."""
    import time as _time

    from anneal_db.sink import DBStepEventSink
    healer = LadderHealer()
    storage_state = repo.get_storage_state(engine, project_id=project_id, persona=persona,
                                           target_id=target["id"])    # THIS platform's session (§3.5)
    heal_id = repo.start_child_run(engine, parent_run_id=red_run_id, role="heal")
    started = _time.monotonic()
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        br = await p.chromium.launch()
        try:
            async with recording(br, artifacts, heal_id, base_url=target["base_url"],
                                 storage_state=storage_state) as (page, ev):
                outcome = await Interpreter.warm(page, plan, DBStepEventSink(engine), artifacts,
                                                 healer=healer, token=token,
                                                 run_result_id=heal_id,
                                                 video_epoch=ev.video_epoch).run()
        finally:
            await br.close()
    repo.finish_child_run(engine, run_result_id=heal_id, verdict=outcome.verdict,
                          error_class=outcome.error_class, trace_path=ev.trace_path,
                          video_path=ev.video_path, screenshot_path=ev.screenshot_path,
                          network_log_path=ev.network_log_path,
                          duration_ms=int((_time.monotonic() - started) * 1000))
    if outcome.verdict not in ("green", "green_with_heals") or not outcome.heals:
        return False

    head = repo.get_test(engine, test_id=test_id)["head_version"]
    test_key = repo.get_test(engine, test_id=test_id)["test_key"]
    new_bundle = json.loads(json.dumps(repo.load_bundle(engine, test_id=test_id, version=head)))
    by_id = {s["id"]: s for s in new_bundle["steps"]}
    for h in outcome.heals:
        st = by_id.get(h["step_id"])
        if st is None:
            continue
        if st.get("action") == "assert":
            st["assert"]["target"] = h["target"]
        else:
            st["target"] = h["target"]
    new_v = repo.create_test_version(engine, test_id=test_id, bundle=new_bundle, reason="warm_heal",
                                     parent_version=head, verified_against=target["key"], state="frozen",
                                     triggered_by="heal", apply=True)
    plan_by_id = {s.step_id: s for s in plan.steps}
    for h in outcome.heals:
        planstep = plan_by_id.get(h["step_id"])
        sig = step_signature(planstep) if planstep else f"-|-|{h['step_id']}||"
        iid, _ = incidents.attach(engine, project_id=project_id, klass="locator", target=target,
                                  step_sig=sig, error_class="LOCATOR_DRIFT", test_id=test_id, red_run_id=None)
        incidents.close_heal(engine, project_id=project_id, incident_id=iid,
                             summary=f"{test_key} {h['step_id']}: {h['summary']} (warm heal, v{new_v})")
    return True


async def on_red_run(engine, artifacts, *, project_id, target, test_id, run_result_id, outcome, plan,
                     bundle, token, modules, persona) -> str:
    """Route a repeatable red. Returns the D29 run verdict."""
    lane = route(outcome.error_class)
    sig = _sig_for(plan, outcome.failed_step_id)

    if lane is Lane.PARK:
        return "parked_auth" if outcome.error_class == "AUTH_EXPIRED" else "parked_env"
    if lane in (Lane.STOP, Lane.NONE):
        return "red"
    if lane is Lane.REVIEW:
        incidents.attach(engine, project_id=project_id, klass="review", target=target, step_sig=sig,
                         error_class=outcome.error_class, test_id=test_id, red_run_id=run_result_id)
        return "red"

    # oscillation stop (D27): the SAME cluster keeps coming back on this test → quarantine, spend nothing.
    if incidents.oscillating(engine, test_id=test_id, target=target, step_sig=sig,
                             error_class=outcome.error_class):
        incidents.quarantine(engine, project_id=project_id, test_id=test_id, target=target,
                             step_sig=sig, error_class=outcome.error_class)
        return "red"

    if lane is Lane.HEAL:
        healed = await _heal_and_apply(engine, artifacts, project_id=project_id, target=target,
                                       test_id=test_id, red_run_id=run_result_id, plan=plan,
                                       token=token, persona=persona)
        if healed:
            return "green_with_heals"
        incidents.attach(engine, project_id=project_id, klass="locator", target=target, step_sig=sig,
                         error_class="LOCATOR_DRIFT", test_id=test_id, red_run_id=run_result_id)
        return "red"

    # lane is REAUTHOR (behavioral)
    iid, created = incidents.attach(engine, project_id=project_id, klass="behavior", target=target,
                                    step_sig=sig, error_class=outcome.error_class, test_id=test_id,
                                    red_run_id=run_result_id)
    # the discoverer (the red that OPENED the incident) re-authors BLIND and publishes the change brief;
    # every later member re-authors GUIDED by it (§9.4, the "1 discovery + N guided" cost shape). A guided
    # member waits (event-driven) for the brief so this holds under real parallelism, not only serial runs.
    brief = None if created else await _await_brief(engine, iid)
    res = None
    try:
        res = await reauthor_member(engine, artifacts, project_id=project_id, target=target, test_id=test_id,
                                    nl=_nl_of(bundle), incident_id=iid, modules=modules,
                                    red_run_id=run_result_id, change_brief=brief, persona=persona)
        # Re-election (§9.4): the "representative" is whoever FIRST verifies green, not rigidly the opener.
        # Publish the brief HERE — inside the try, BEFORE the finally signals rep_concluded — so on the
        # discoverer's SUCCESS path a waiter can never observe concluded-without-brief (the brief is already
        # visible). If the opener failed, the next member to verify publishes instead, so later members stay
        # guided and nobody stalls on a brief that will never come.
        if res is not None and res["verified"]:
            inc_now = repo.get_incident(engine, incident_id=iid)
            if not (inc_now and inc_now.get("change_brief")):
                repo.set_incident(engine, incident_id=iid, change_brief=res["change_brief"], state="propagating")
    finally:
        if created:
            # the discoverer has concluded on EVERY exit path — success, honest failure, budget-exhaust, OR
            # a crash/engine-fault (this finally runs before the exception propagates on to ENGINE_FAULT).
            # Signalling here lets guided waiters break instead of stalling. Guard it so a signalling failure
            # can never MASK the discoverer's real exception/verdict — the fault must stay loud; a missed
            # signal only degrades a waiter to the ceiling backstop.
            try:
                repo.set_rep_concluded(engine, incident_id=iid)
            except Exception:  # noqa: BLE001
                pass
    if res is None:                                   # budget exhausted → partial results at the inbox
        repo.changelog_add(engine, project_id=project_id, kind="budget_exhausted",
                           summary=f"incident {iid}: agent-run budget exhausted; partial results",
                           incident_id=iid)
        return "red"
    return "provisional" if res["verified"] else "red"
