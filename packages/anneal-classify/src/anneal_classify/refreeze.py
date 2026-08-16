"""Fallback-rung re-freeze (D28, §6.1) — DETERMINISTIC, no LLM.

A cold run that passed a step on a fallback rung (rung_used ≠ the plan's top rung) is still green, but
it is green *on borrowed time* — the primary rung has rotted. So each such pass auto-opens a re-freeze
incident and applies vN+1 that PROMOTES the rung that actually worked to primary (demoting the dead one
to a fallback). The incident is locator-class → it auto-closes as one changelog line (§8). Next run
passes on the primary rung: convergence, on the ledger.
"""
from __future__ import annotations

from anneal_core.signatures import step_signature
from anneal_db import repositories as repo

from . import incidents


def _promote(target: dict, rung_used: str) -> dict:
    """Promote the surviving rung to primary; demote the dead primary into fallbacks. A role rung keeps
    its accessible name in fallbacks.text (derive_rungs reads the name from there, §2.3)."""
    if not rung_used or target.get("strategy") == rung_used:
        return target
    fb = dict(target.get("fallbacks") or {})
    if rung_used not in fb:
        return target                       # nothing to promote (rung wasn't a captured fallback)
    new_sel = fb.pop(rung_used)
    fb.setdefault(target["strategy"], target["selector"])   # keep the old primary as a (now stale) fallback
    out = {k: v for k, v in target.items() if k not in ("strategy", "selector", "fallbacks")}
    out["strategy"], out["selector"] = rung_used, new_sel
    if fb:
        out["fallbacks"] = fb
    return out


def _step_target(step: dict) -> dict | None:
    if step.get("action") == "assert":
        return (step.get("assert") or {}).get("target")
    return step.get("target")


def _set_step_target(step: dict, target: dict):
    if step.get("action") == "assert":
        step["assert"]["target"] = target
    else:
        step["target"] = target


def refreeze_fallbacks(engine, *, project_id, target, test_id, run_result_id, plan, bundle) -> list[dict]:
    """For every fallback-rung pass in the run, promote the working rung in a fresh vN+1 (applied),
    and open→close a locator incident + one changelog line. Returns the promotions performed."""
    fbs = repo.fallback_steps(engine, run_result_id=run_result_id)
    if not fbs:
        return []

    plan_by_id = {s.step_id: s for s in plan.steps}
    new_bundle = _deep_copy(bundle)
    steps_by_id = {s["id"]: s for s in new_bundle["steps"]}
    promotions = []
    for fb in fbs:
        sid, rung = fb["step_id"], fb["rung_used"]
        step = steps_by_id.get(sid)
        if step is None:
            continue
        tgt = _step_target(step)
        if tgt is None:
            continue
        promoted = _promote(tgt, rung)
        if promoted is tgt:
            continue
        _set_step_target(step, promoted)
        promotions.append({"step_id": sid, "rung_used": rung,
                           "old": tgt.get("strategy"), "new": promoted["strategy"]})

    if not promotions:
        return []

    head = repo.get_test(engine, test_id=test_id)["head_version"]
    new_version = repo.create_test_version(
        engine, test_id=test_id, bundle=new_bundle, reason="fallback_refreeze", parent_version=head,
        verified_against=target["key"], state="frozen", triggered_by="heal", apply=True)

    test_key = repo.get_test(engine, test_id=test_id)["test_key"]
    for p in promotions:
        planstep = plan_by_id.get(p["step_id"])
        sig = step_signature(planstep) if planstep else f"-|-|{p['step_id']}||"
        iid, _ = incidents.attach(engine, project_id=project_id, klass="locator", target=target,
                                  step_sig=sig, error_class="LOCATOR_DRIFT", test_id=test_id,
                                  red_run_id=run_result_id)
        summary = (f"{test_key} {p['step_id']}: primary rung {p['old']}→{p['new']} "
                   f"(healed via fallback, re-frozen v{new_version})")
        incidents.close_heal(engine, project_id=project_id, incident_id=iid, summary=summary)

    return promotions


def _deep_copy(obj):
    import json
    return json.loads(json.dumps(obj))
