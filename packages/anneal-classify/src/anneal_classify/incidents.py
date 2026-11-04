"""The §9 incident state machine (§8 table, D27 budgets, D37 ruling_note).

    open ─ klass=locator ─ heal auto-applies per member (D3) ───────────────────▶ closed (changelog line)
    open ─ klass=behavior ─ representative re-author ▶ provisional ▶ propagating
                                        └─ ruling{feature|bug}+ruling_note ─▶ ruled ─▶ closed

Attach is the 3.1a upsert (N reds converge to one open incident); spend is the 3.1b atomic budget
guard; a ruling is web-only (enforced at the API, D32). A heal/fallback recurring on the same
step_signature trips the oscillation stop (D27): quarantine + open incident, spend nothing.
"""
from __future__ import annotations

from anneal_db import repositories as repo

# Defaults if TargetProfile doesn't pin them (D27). cap = 1 discovery + M guided + K verify, coarse.
_DEFAULT_CAP_AGENT_RUNS = 8
_DEFAULT_CAP_LLM_CALLS = 200


def caps_for(target: dict) -> tuple[int, int]:
    prof = target.get("profile") or {}
    return (int(prof.get("cap_agent_runs", _DEFAULT_CAP_AGENT_RUNS)),
            int(prof.get("cap_llm_calls", _DEFAULT_CAP_LLM_CALLS)))


def cluster_key(target: dict, step_sig: str, error_class: str) -> tuple[str, str, str]:
    """(surface, step_signature, error_class). Surface = the route family where the failure occurred;
    the probe_path is the stable route handle for this target (a per-step route refinement is a later
    tightening — the step_signature already separates distinct failing steps within the route)."""
    surface = (target.get("profile") or {}).get("probe_path") or "/"
    return surface, step_sig, error_class


def attach(engine, *, project_id, klass, target, step_sig, error_class, test_id, red_run_id):
    """3.1a. Returns (incident_id, created)."""
    surface, sig, ec = cluster_key(target, step_sig, error_class)
    cap_a, cap_l = caps_for(target)
    return repo.attach_incident(engine, project_id=project_id, klass=klass, surface=surface,
                                step_sig=sig, error_class=ec, cap_agent_runs=cap_a, cap_llm_calls=cap_l,
                                test_id=test_id, red_run_id=red_run_id)


def spend_agent_run(engine, incident_id) -> bool:
    return repo.spend_agent_run(engine, incident_id=incident_id)


def close_heal(engine, *, project_id, incident_id, summary):
    """§8: a locator/heal incident auto-resolves — one changelog line, incident closed (D3)."""
    repo.changelog_add(engine, project_id=project_id, kind="heal", summary=summary, incident_id=incident_id)
    repo.set_incident(engine, incident_id=incident_id, state="closed", closed=True)


def rule(engine, *, project_id, incident_id, ruling, ruling_note, ruled_by):
    """Uniform ruling — ONE decision for every member (the inbox "Rule all Feature/Bug" action).
    Delegates to rule_members with the same decision per member (§9.5). WEB-ONLY (D32)."""
    if ruling not in ("feature", "bug"):
        raise ValueError(f"ruling must be feature|bug, got {ruling!r}")
    inc = repo.get_incident(engine, incident_id=incident_id)
    if inc is None:
        raise ValueError("no such incident")
    members = repo.incident_members(engine, incident_id=incident_id)
    return rule_members(engine, project_id=project_id, incident_id=incident_id,
                        members={str(m["test_id"]): ruling for m in members},
                        ruling_note=ruling_note, ruled_by=ruled_by)


def rule_members(engine, *, project_id, incident_id, members, ruling_note, ruled_by):
    """Per-member ruling (A5, money-shot Direction B) — one ATOMIC transition, each member decided
    feature|bug individually; `members` = {test_id: ruling}. The incident-level ruling is DERIVED
    (feature|bug|mixed). There is no partially-ruled state: one POST rules the incident.

    feature → that member's provisional version becomes canonical (state='frozen'); head stays on it.
    bug     → that member's provisional rolls back (head reverts to its parent); one bug filed carrying
              the bug-ruled members (C5). Either way ruling_note lands on the incident AND the changelog —
              the 'why' the next re-author agent reads when this surface drifts again (never a bare flag).
    """
    bad = {t: r for t, r in members.items() if r not in ("feature", "bug")}
    if bad:
        raise ValueError(f"member rulings must be feature|bug, got {bad!r}")
    inc = repo.get_incident(engine, incident_id=incident_id)
    if inc is None:
        raise ValueError("no such incident")
    if inc["state"] in ("ruled", "closed"):
        raise ValueError(f"incident already {inc['state']}")
    actual = {str(m["test_id"]): m for m in repo.incident_members(engine, incident_id=incident_id)}
    unknown = set(members) - set(actual)
    if unknown:
        raise ValueError(f"not members of this incident: {sorted(unknown)}")

    decided = sorted(set(members.values()))
    derived = decided[0] if len(decided) == 1 else "mixed"

    affected, applied, bugged = [], {}, []
    for tid, ruling in members.items():
        m = actual[tid]
        # the provisional version this incident applied for the member (highest provisional under it)
        prov = _latest_provisional(engine, tid, incident_id)
        if prov is None:
            continue
        affected.append(m["test_key"])
        applied[tid] = ruling
        repo.set_member_ruling(engine, incident_id=incident_id, test_id=tid, ruling=ruling)
        if ruling == "feature":
            repo.set_version_state(engine, test_id=tid, version=prov["version"], state="frozen",
                                   ruling="feature")
            repo.set_head_version(engine, test_id=tid, version=prov["version"])
        else:  # bug → roll back to the provisional's parent
            bugged.append(m["test_key"])
            parent = prov.get("parent_version")
            repo.set_version_state(engine, test_id=tid, version=prov["version"], state="rolled_back",
                                   ruling="bug")
            if parent is not None:
                repo.set_head_version(engine, test_id=tid, version=parent)

    # 'ruled' is written only AFTER every member moved: a crash mid-loop leaves the incident re-rulable
    # (the guard above refuses ruled/closed), so a re-POST completes idempotently instead of sticking.
    repo.set_incident(engine, incident_id=incident_id, state="ruled", ruling=derived,
                      ruling_note=ruling_note, ruled_by=ruled_by, ruled=True)
    summary = (f"{inc['ck_step_sig']} on {inc['ck_surface']}: ruled {derived} "
               f"({len(affected)} test(s): {', '.join(affected)})")
    repo.changelog_add(engine, project_id=project_id,
                       kind=f"behavior_ruling:{derived}", summary=summary,
                       incident_id=incident_id, ruling_note=ruling_note)
    if bugged:
        # C5: a structured bug carrying the bug-ruled members as affected coverage.
        repo.changelog_add(engine, project_id=project_id, kind="bug",
                            summary=f"PRODUCT_BUG: {inc['ck_step_sig']} on {inc['ck_surface']}: "
                                    f"ruled bug ({len(bugged)} test(s): {', '.join(bugged)})",
                            incident_id=incident_id, ruling_note=ruling_note)
    repo.set_incident(engine, incident_id=incident_id, state="closed", closed=True)
    return {"ruling": derived, "affected": affected, "ruling_note": ruling_note, "members": applied}


def _latest_provisional(engine, test_id, incident_id):
    versions = repo.list_test_versions(engine, test_id=test_id)
    provs = [v for v in versions if v["state"] == "provisional"
             and str(v.get("incident_id") or "") == str(incident_id)]
    return provs[-1] if provs else None


def quarantine(engine, *, project_id, test_id, target, step_sig, error_class):
    """D27 oscillation stop: quarantine the test, open a DISTINCT oscillation incident, spend nothing.
    Its cluster uses a `::oscillation` error_class suffix so it never converges onto (and is never
    counted as another recurrence of) the live locator/behavior incident it is quarantining."""
    repo.set_quarantined(engine, test_id=test_id, quarantined=True)
    iid, _ = attach(engine, project_id=project_id, klass="oscillation", target=target,
                    step_sig=step_sig, error_class=f"{error_class}::oscillation",
                    test_id=test_id, red_run_id=None)
    repo.changelog_add(engine, project_id=project_id, kind="quarantine",
                       summary=f"oscillation stop on {step_sig} ({error_class}) — quarantined",
                       test_id=test_id, incident_id=iid)
    return iid


def oscillating(engine, *, test_id, target, step_sig, error_class, threshold=2) -> bool:
    """True when the SAME cluster key — (surface, step_signature, error_class) — has ALREADY driven
    `threshold` incidents for this test: a heal/fallback/behavior fix that keeps coming back on the
    same step rather than converging (D27). Keyed on the full cluster (NOT bare error_class), so a
    test that drifts on three DIFFERENT steps is never falsely quarantined; counted from incidents
    (not run verdicts), so runs that CONVERGED green never count as recurrences."""
    surface, sig, ec = cluster_key(target, step_sig, error_class)
    return repo.count_test_cluster_incidents(engine, test_id=test_id, surface=surface,
                                             step_sig=sig, error_class=ec) >= threshold
