"""M3 (marker: m3) — the RED path. Proves HLD §6/§9 against the DRIFTED golden app:

- a fallback-rung pass auto-re-freezes (promotes the surviving rung) and heals as ONE changelog line;
- the warm lane drains MULTIPLE dead-ladder drifts in one session (D28), ladder-first then LLM-over-DOM;
- a behavioral drift (new required field, 422 enforced) reds N tests into ONE incident, re-authors a
  representative + guided members to provisional, and a single ruling propagates (feature) or rolls
  back (bug) — carrying ruling_note (D37); rulings are WEB-ONLY (D32);
- budgets cap agent spend (partial results, never a retry storm) and oscillation quarantines (D27).

Drift is flipped over HTTP on the hermetic golden app; frozen bundles are hand-authored (the freeze is
the determinism boundary — M3 exercises the red path GIVEN frozen tests, as M1 did for the cold lane).
"""
from __future__ import annotations

import json
import urllib.request

import pytest
from sqlalchemy import text

from anneal_artifacts.store import LocalDiskStore
from anneal_core.bundle import Bundle
from anneal_db import repositories as repo
from anneal_db.engine import get_engine
from anneal_scheduler.scheduler import run_suite

from conftest import GOLDEN_URL

# delete children before parents (FK: incident_member.red_run_id→run_result, *→incident/test/…)
_TABLES = ["step_event", "incident_member", "run_result", "run_task", "test_version", "test_golden",
           "suite_run", "suite", "changelog", "incident", "test", "module_version", "module", "session",
           "credential", "territory_notes", "target", "project"]


# ── drift control + residue reset on the golden app ──────────────────────────────────────────────
def _set_drift(**flags):
    body = json.dumps(flags).encode()
    req = urllib.request.Request(f"{GOLDEN_URL}/__drift", data=body,
                                 headers={"content-type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()


def _reset_campaigns():
    # restore the golden app to its initial hermetic state (seed row present, drift off)
    req = urllib.request.Request(f"{GOLDEN_URL}/__reset", data=b"{}",
                                 headers={"content-type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()


# ── frozen-bundle builders (hand-authored; test data, target-specific is allowed here) ───────────
def _bundle(test_key, steps, nl, *, mutating=False):
    b = {"schema_version": 1, "project_id": "golden", "test_id": test_key, "version": 1,
         "state": "frozen", "persona": "member", "params": {},
         "lineage": {"parent": None, "reason": "authored", "ruling": "canonical", "incident": None,
                     "triggered_by": "m3_seed", "verified_against": "golden-local"},
         "intent": {"sources": [{"kind": "nl", "verbatim": nl}], "descriptor": nl[:60]},
         "policy": {"exclusive": False, "mutating": mutating}, "steps": steps}
    Bundle.model_validate(b)
    return b


def _goto_campaigns():
    return {"id": "s1", "intent": "open the campaigns list", "action": "goto", "arg": "/campaigns",
            "postcondition": {"all": [{"kind": "element", "state": "visible",
                "target": {"strategy": "testid", "selector": "campaigns-table",
                           "fallbacks": {"role": "table"}}}], "budget_ms": 10000}}


def _click_add(*, with_fallback):
    # with_fallback=True → testid drift heals via the role rung (fallback pass). False → dead ladder → warm.
    tgt = {"strategy": "testid", "selector": "btn-add-campaign"}
    if with_fallback:
        tgt["fallbacks"] = {"role": "button", "text": "Add Campaign"}
    return {"id": "s2", "intent": "open the create form", "action": "click", "target": tgt,
            "postcondition": {"all": [{"kind": "element", "state": "visible",
                "target": {"strategy": "testid", "selector": "input-campaign-name",
                           "fallbacks": {"placeholder": "Campaign name"}}}], "budget_ms": 8000}}


HEAL_FB_NL = "Open the campaigns list and start creating a campaign."
HEAL_DRAIN_NL = "Open campaigns and create one — open the form, name it, and submit."
CREATE_NL = "Create a new campaign, filling every required field, and verify it opens."


def bundle_heal_fallback():
    return _bundle("heal-fb", [_goto_campaigns(), _click_add(with_fallback=True)], HEAL_FB_NL)


def bundle_heal_drain():
    # two testid-only buttons (Add, Submit) both die under testid drift → the warm lane heals BOTH in
    # one session (D28). Both are <button aria-label=…> so the LLM re-observation is unambiguous.
    steps = [
        _goto_campaigns(),
        _click_add(with_fallback=False),                        # s2: dead under testid → warm heal
        {"id": "s3", "intent": "fill the campaign name", "action": "fill",
         "target": {"strategy": "testid", "selector": "input-campaign-name",
                    "fallbacks": {"placeholder": "Campaign name"}},
         "arg": "M3 {{run.token}}", "postcondition": None, "budget_ms": 8000},
        {"id": "s4", "intent": "submit the create form", "action": "click",
         "target": {"strategy": "testid", "selector": "btn-campaign-create-submit"},  # dead under testid
         "postcondition": {"all": [{"kind": "url", "match": ".*/campaign-v2/[^/]+"}], "budget_ms": 8000}},
    ]
    return _bundle("heal-drain", steps, HEAL_DRAIN_NL, mutating=True)


def bundle_create(test_key):
    steps = [
        _goto_campaigns(),
        _click_add(with_fallback=True),
        {"id": "s3", "intent": "fill the campaign name", "action": "fill",
         "target": {"strategy": "testid", "selector": "input-campaign-name",
                    "fallbacks": {"placeholder": "Campaign name"}},
         "arg": "M3 {{run.token}}", "postcondition": None, "budget_ms": 8000},
        {"id": "s4", "intent": "submit the create form", "action": "click",
         "target": {"strategy": "testid", "selector": "btn-campaign-create-submit",
                    "fallbacks": {"role": "button", "text": "Create"}},
         "postcondition": {"all": [{"kind": "url", "match": ".*/campaign-v2/[^/]+"}], "budget_ms": 8000}},
    ]
    return _bundle(test_key, steps, CREATE_NL, mutating=True)


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def env():
    eng = get_engine()
    with eng.begin() as c:
        for t in _TABLES:
            c.execute(text(f"delete from {t}"))
    tid = repo.seed_project_target(eng, project_id="golden", project_name="Golden",
                                   target_key="golden-local", base_url=GOLDEN_URL, timezone_="UTC",
                                   profile={"probe_path": "/campaigns"})
    _set_drift()                # reset all drift
    _reset_campaigns()
    yield {"engine": eng, "target_id": tid}
    _set_drift()


def _seed_test(eng, bundle):
    tid, _ = repo.upsert_test(eng, project_id="golden", test_key=bundle["test_id"], bundle=bundle,
                              verified_against="golden-local")
    return tid


def _run(eng, target_id, suite_id, max_parallel=1):
    art = LocalDiskStore("/tmp/anneal-m3art")
    import asyncio
    sr = repo.create_suite_run(eng, project_id="golden", suite_id=suite_id, target_id=target_id)
    return asyncio.run(run_suite(eng, art, suite_run_id=sr, target_id=target_id, max_parallel=max_parallel))


# ═══════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.m3
def test_fallback_rung_pass_refreezes_and_heals_as_changelog(env):
    """testid rename: the primary rung rots, the role fallback carries the run green_with_heals, and a
    locator incident auto-closes as ONE changelog line while vN+1 promotes the surviving rung (§6.1/D3)."""
    eng, tid = env["engine"], env["target_id"]
    test_id = _seed_test(eng, bundle_heal_fallback())
    suite = repo.create_suite(eng, project_id="golden", name="heal-fb", member_test_ids=[test_id])

    _set_drift(testid=True)
    report = _run(eng, tid, suite)
    assert report.verdict == "green_with_heals", report

    incs = repo.list_incidents(eng, project_id="golden")
    loc = [i for i in incs if i["klass"] == "locator"]
    assert loc and all(i["state"] == "closed" for i in loc), incs
    heals = [c for c in repo.list_changelog(eng, project_id="golden") if c["kind"] == "heal"]
    assert heals, "no heal changelog line"

    # vN+1 applied, and the promoted step now leads with the role rung (convergence, not still-testid)
    head = repo.get_test(eng, test_id=test_id)["head_version"]
    assert head == 2, head
    new_bundle = repo.load_bundle(eng, test_id=test_id, version=head)
    s2 = next(s for s in new_bundle["steps"] if s["id"] == "s2")
    assert s2["target"]["strategy"] == "role", s2["target"]


@pytest.mark.m3
def test_warm_lane_drains_all_drifts_in_one_session(env):
    """testid rename kills the whole ladder on TWO steps (Add + Submit): cold reds, then the warm lane
    heals BOTH in ONE session (D28, §6.3) via LLM-over-DOM re-observation and re-freezes each —
    green_with_heals, two heal changelog lines, one vN+1."""
    eng, tid = env["engine"], env["target_id"]
    test_id = _seed_test(eng, bundle_heal_drain())
    suite = repo.create_suite(eng, project_id="golden", name="heal-drain", member_test_ids=[test_id])

    _set_drift(testid=True)
    report = _run(eng, tid, suite)
    assert report.verdict == "green_with_heals", report

    head = repo.get_test(eng, test_id=test_id)["head_version"]
    assert head == 2, f"warm heal did not apply one vN+1: head={head}"
    heals = [c for c in repo.list_changelog(eng, project_id="golden") if c["kind"] == "heal"]
    assert len(heals) >= 2, f"expected ≥2 heal lines (drain-all-drifts), got {heals}"
    new_bundle = repo.load_bundle(eng, test_id=test_id, version=head)
    s2 = next(s for s in new_bundle["steps"] if s["id"] == "s2")
    s4 = next(s for s in new_bundle["steps"] if s["id"] == "s4")
    # both buttons were re-observed to a live, working rung (their new testid or role/aria signal)
    assert s2["target"]["strategy"] in ("role", "testid"), s2["target"]
    assert s4["target"]["strategy"] in ("role", "testid"), s4["target"]
    _reset_campaigns()


@pytest.mark.m3
def test_behavior_incident_one_ruling_propagates_to_members(env):
    """require_dates (422 enforced): two create tests red into ONE behavior incident; the representative
    re-authors blind (+ change_brief), the member re-authors guided; both go provisional; a WEB-ONLY
    feature ruling with a note propagates to BOTH — every provisional version becomes canonical (§9)."""
    from anneal_api.app import create_app
    from fastapi.testclient import TestClient

    eng, tid = env["engine"], env["target_id"]
    a = _seed_test(eng, bundle_create("create-a"))
    b = _seed_test(eng, bundle_create("create-b"))
    suite = repo.create_suite(eng, project_id="golden", name="creates", member_test_ids=[a, b])

    _set_drift(require_dates=True)
    # run serial to keep two live re-authors off one loaded laptop; a guided member also WAITS for the
    # representative's brief (redpath._await_brief), so propagation is correct under parallelism too.
    report = _run(eng, tid, suite, max_parallel=1)
    assert report.verdict == "provisional", report          # provisional counts as pass (D29)

    incs = [i for i in repo.list_incidents(eng, project_id="golden") if i["klass"] == "behavior"]
    assert len(incs) == 1, f"expected ONE behavior incident, got {len(incs)}"
    inc = incs[0]
    assert inc["state"] == "propagating" and inc["change_brief"], inc
    members = repo.incident_members(eng, incident_id=inc["id"])
    assert {m["test_key"] for m in members} == {"create-a", "create-b"}, members
    # both members are on a provisional version, applied immediately (§9.4)
    for t in (a, b):
        vs = repo.list_test_versions(eng, test_id=t)
        assert any(v["state"] == "provisional" for v in vs), (t, vs)
    # the §9 cost shape: exactly ONE member re-authored BLIND (the representative), the other GUIDED —
    # proven by the version reason, so the change-brief propagation is real, not a no-op under serial runs
    reasons = [v["reason"] for t in (a, b) for v in repo.list_test_versions(eng, test_id=t) if v["version"] > 1]
    assert reasons.count("reauthor_guided") == 1, f"expected exactly one GUIDED member re-author, got {reasons}"
    assert reasons.count("reauthor") == 1, f"expected exactly one BLIND (representative) re-author, got {reasons}"

    # WEB-ONLY ruling (D32): feature → all provisionals canonical, carrying the note
    client = TestClient(create_app(engine=eng))
    r = client.post(f"/incidents/{inc['id']}/ruling",
                    json={"ruling": "feature", "ruling_note": "intended: dates now mandatory", "ruled_by": "sid"})
    assert r.status_code == 200, r.text
    assert set(r.json()["affected"]) == {"create-a", "create-b"}
    inc2 = repo.get_incident(eng, incident_id=inc["id"])
    assert inc2["state"] == "closed" and inc2["ruling"] == "feature"
    assert inc2["ruling_note"] == "intended: dates now mandatory"
    for t in (a, b):
        vs = repo.list_test_versions(eng, test_id=t)
        prov = [v for v in vs if v["ruling"] == "feature"]
        assert prov and all(v["state"] == "frozen" for v in prov), (t, vs)
    ruling_log = [c for c in repo.list_changelog(eng, project_id="golden")
                  if c["kind"].startswith("behavior_ruling")]
    assert ruling_log and ruling_log[0]["ruling_note"] == "intended: dates now mandatory"


@pytest.mark.m3
def test_ruling_bug_rolls_back_and_files_a_bug(env):
    """A single create test reds under require_dates → provisional; a WEB-ONLY bug ruling rolls the
    provisional back (head reverts to the parent) and files one bug carrying the member (C5), with note."""
    from anneal_classify import incidents as inc_machine

    eng, tid = env["engine"], env["target_id"]
    a = _seed_test(eng, bundle_create("create-solo"))
    suite = repo.create_suite(eng, project_id="golden", name="solo", member_test_ids=[a])

    _set_drift(require_dates=True)
    report = _run(eng, tid, suite)
    assert report.verdict == "provisional", report
    inc = [i for i in repo.list_incidents(eng, project_id="golden") if i["klass"] == "behavior"][0]
    assert repo.get_test(eng, test_id=a)["head_version"] == 2   # provisional applied

    out = inc_machine.rule(eng, project_id="golden", incident_id=inc["id"], ruling="bug",
                           ruling_note="real regression — dates should not be required", ruled_by="sid")
    assert out["ruling"] == "bug"
    assert repo.get_test(eng, test_id=a)["head_version"] == 1, "bug ruling did not roll head back to parent"
    vs = repo.list_test_versions(eng, test_id=a)
    assert any(v["version"] == 2 and v["state"] == "rolled_back" for v in vs), vs
    bugs = [c for c in repo.list_changelog(eng, project_id="golden") if c["kind"] == "bug"]
    assert bugs and bugs[0]["ruling_note"] == "real regression — dates should not be required"


@pytest.mark.m3
def test_budget_cap_surfaces_partial_never_retry_storm(env):
    """cap_agent_runs=1: the representative re-author spends the only run; the second member's re-author
    is refused by the atomic budget guard (3.1b) → partial results at the inbox, no silent retry storm."""
    eng = env["engine"]
    tid = repo.seed_project_target(eng, project_id="golden", project_name="Golden",
                                   target_key="golden-cap1", base_url=GOLDEN_URL, timezone_="UTC",
                                   profile={"probe_path": "/campaigns", "cap_agent_runs": 1,
                                            "cap_llm_calls": 200})
    a = _seed_test(eng, bundle_create("cap-a"))
    b = _seed_test(eng, bundle_create("cap-b"))
    suite = repo.create_suite(eng, project_id="golden", name="cap", member_test_ids=[a, b])

    _set_drift(require_dates=True)
    _run(eng, tid, suite, max_parallel=1)

    inc = [i for i in repo.list_incidents(eng, project_id="golden") if i["klass"] == "behavior"][0]
    assert inc["budget_agent_runs"] == inc["cap_agent_runs"] == 1, inc
    exhausted = [c for c in repo.list_changelog(eng, project_id="golden") if c["kind"] == "budget_exhausted"]
    assert exhausted, "budget exhaustion was not surfaced (silent retry storm risk)"
    # the guard is a HARD ceiling: at most one member re-authored, and at least one was DENIED (the
    # real budget guarantee — a partial result, never a runaway retry storm) — D27.
    provisioned = sum(any(v["version"] > 1 for v in repo.list_test_versions(eng, test_id=t)) for t in (a, b))
    denied = sum(all(v["version"] == 1 for v in repo.list_test_versions(eng, test_id=t)) for t in (a, b))
    assert provisioned <= 1, f"budget cap breached: {provisioned} members re-authored under cap=1"
    assert denied >= 1, "budget cap denied no member — the second re-author was not actually blocked"


@pytest.mark.m3
def test_oscillation_quarantines_on_recurring_signature_only(env):
    """The oscillation stop (D27) keys on the SAME cluster (surface, step_signature, error_class): a
    fix that keeps coming back on ONE step quarantines the test; drift on a DIFFERENT step does not.
    Quarantine opens a DISTINCT oscillation incident (never converging onto the live one), spending
    nothing more."""
    from anneal_classify import incidents as inc_machine

    eng, tid = env["engine"], env["target_id"]
    test_id = _seed_test(eng, bundle_create("osc"))
    target = repo.get_target(eng, target_id=tid)
    step_sig, ec = "|click|testid|btn-campaign-create-submit|", "POSTCONDITION_UNMET"
    surface, sig, ecc = inc_machine.cluster_key(target, step_sig, ec)

    # simulate the SAME cluster recurring: two prior (now-closed) incidents this test was a member of
    for _ in range(2):
        iid, _ = repo.attach_incident(eng, project_id="golden", klass="behavior", surface=surface,
                                      step_sig=sig, error_class=ecc, cap_agent_runs=8, cap_llm_calls=200,
                                      test_id=test_id, red_run_id=None)
        repo.set_incident(eng, incident_id=iid, state="closed", closed=True)   # close so the next opens

    # recurring on the SAME step → oscillating; a DIFFERENT step signature → NOT oscillating (keying works)
    assert inc_machine.oscillating(eng, test_id=test_id, target=target, step_sig=step_sig, error_class=ec)
    assert not inc_machine.oscillating(eng, test_id=test_id, target=target,
                                       step_sig="|click|testid|btn-somewhere-else|", error_class=ec)

    inc_machine.quarantine(eng, project_id="golden", test_id=test_id, target=target,
                           step_sig=step_sig, error_class=ec)
    assert repo.get_test(eng, test_id=test_id)["quarantined"] is True
    q = [i for i in repo.list_incidents(eng, project_id="golden") if i["klass"] == "oscillation"]
    assert len(q) == 1 and q[0]["ck_error_class"].endswith("::oscillation"), q


@pytest.mark.m3
def test_ruling_is_web_only_cli_cannot_reach_it(env):
    """D32: the ruling lives in front of screenshots/video — the CLI exposes no command that reaches it."""
    from anneal_cli import main as cli
    # the CLI has no `ruling` subcommand: argparse rejects it before any network call (SystemExit).
    with pytest.raises(SystemExit):
        cli.main(["ruling", "inc-1", "--ruling", "feature"], client=object())
    from anneal_classify.router import Lane, route
    assert route("LOCATOR_DRIFT") is Lane.HEAL
    assert route("POSTCONDITION_UNMET") is Lane.REAUTHOR
    assert route("ENV_INFRA") is Lane.PARK


# ── recon-time behavioral progressive disclosure (Option 1): a create form behind a click-to-reveal
#    portal overlay is disclosed at recon so the scaffold can plan it. `reveal_create` is an additive
#    drift; the steady (inline showForm) create flow M2/M3 use is unchanged. ─────────────────────────
REVEAL_NL = ("Create a new campaign, giving it any name, and submit the form to create it. Then verify we "
             "land on the new campaign's detail page showing that name.")


@pytest.mark.m3
def test_reveal_probe_ignores_inline_form_but_catches_overlay(env):
    """Gate #1 + reveal detection: the reveal probe records a create form only when it is behind an
    OVERLAY (fixed/absolute portal) or a route — NEVER an in-flow inline show/hide. So the steady (inline)
    app's recon is UNPERTURBED (persisted territory byte-identical: no reveal recorded), while the
    portal-overlay variant's hidden form IS disclosed. The probe is read-only and restores the page to its
    baseline signature exactly (gate #2)."""
    import asyncio
    from anneal_authoring.author import _elem_sig, _probe_reveals
    from anneal_authoring.perception import snapshot_interactive
    from playwright.async_api import async_playwright

    async def _probe():
        async with async_playwright() as p:
            b = await p.chromium.launch()
            try:
                pg = await (await b.new_context(base_url=GOLDEN_URL)).new_page()
                await pg.goto("/campaigns", wait_until="domcontentloaded")
                await pg.wait_for_timeout(300)
                elems = await snapshot_interactive(pg)
                sig = _elem_sig(elems)
                reveals = await _probe_reveals(pg, elems, start_path="/campaigns", base_sig=sig)
                return reveals, _elem_sig(await snapshot_interactive(pg)) == sig
            finally:
                await b.close()

    # steady (inline showForm): the reveal is in normal flow → NOT significant → discarded (gate #1)
    _reset_campaigns()
    reveals, restored = asyncio.run(_probe())
    assert reveals == [], f"an in-flow inline reveal must be discarded so steady recon is unchanged: {reveals}"
    assert restored, "the reveal probe must restore steady golden to its baseline signature (gate #2)"

    # portal overlay (reveal_create): exactly one overlay reveal, opened by a create control
    _reset_campaigns()
    _set_drift(reveal_create=True)
    reveals, restored = asyncio.run(_probe())
    assert [r["kind"] for r in reveals] == ["overlay"], f"a portal-overlay create form must be disclosed: {reveals}"
    assert restored, "the reveal probe must restore the overlay variant to its baseline signature (gate #2)"
    _set_drift()


@pytest.mark.m3
def test_reveal_create_authors_behind_portal_and_replays_green(env):
    """Gate #3 (capability): the hot lane authors a create flow whose form lives behind a click-to-reveal
    PORTAL OVERLAY (a plain button → fixed portal, no ARIA). Recon-time disclosure surfaces the fields so
    the scaffold plans open-then-fill; the frozen bundle replays GREEN deterministically (K>=3)."""
    import asyncio
    from anneal_authoring.service import author_and_freeze
    eng, tid = env["engine"], env["target_id"]
    _reset_campaigns()
    _set_drift(reveal_create=True)
    res = asyncio.run(author_and_freeze(eng, project_id="golden", test_key="reveal-create",
                                        nl=REVEAL_NL, target_id=tid, persona="member"))
    assert res.get("status") == "frozen", f"recon-reveal must let the behind-portal form author: {res}"
    assert any(s["action"] == "fill" for s in res["bundle"]["steps"]), \
        "the frozen bundle must fill a field that only exists once the portal is opened"
    suite = repo.create_suite(eng, project_id="golden", name="reveal", member_test_ids=[res["test_id"]])
    verdicts = []
    for _ in range(3):
        _reset_campaigns()
        _set_drift(reveal_create=True)
        verdicts.append(_run(eng, tid, suite).verdict)
    assert len(set(verdicts)) == 1, f"reveal_create replay must be deterministic (K=3): {verdicts}"
    assert all(v in ("green", "green_with_heals") for v in verdicts), f"expected green: {verdicts}"
    _set_drift()


@pytest.mark.m3
def test_reveal_probe_blocks_a_mutating_opener(env):
    """Gate #2 (read-only GUARANTEE): a create-verb opener whose click fires a non-GET write is BLOCKED at
    the network layer during the probe — the write never reaches the target (no residue) — and the control
    is excluded, never authored. Proves a reveal probe cannot mutate a live app, not just detect after."""
    import asyncio
    from anneal_authoring.author import _elem_sig, _probe_reveals
    from anneal_authoring.perception import snapshot_interactive
    from playwright.async_api import async_playwright

    def _names():
        data = json.loads(urllib.request.urlopen(f"{GOLDEN_URL}/api/v2/campaigns").read().decode())
        return sorted(c.get("name", "") for c in data.values())

    _reset_campaigns()
    _set_drift(mutation_trap=True)          # adds an "Add Note" opener whose click POSTs a new campaign
    before = _names()

    async def _probe():
        async with async_playwright() as p:
            b = await p.chromium.launch()
            try:
                pg = await (await b.new_context(base_url=GOLDEN_URL)).new_page()
                await pg.goto("/campaigns", wait_until="domcontentloaded")
                await pg.wait_for_timeout(300)
                elems = await snapshot_interactive(pg)
                sig = _elem_sig(elems)
                reveals = await _probe_reveals(pg, elems, start_path="/campaigns", base_sig=sig)
                return reveals, _elem_sig(await snapshot_interactive(pg)) == sig
            finally:
                await b.close()

    reveals, restored = asyncio.run(_probe())
    assert _names() == before, f"a reveal probe MUST NOT mutate — the trap write was not blocked: {_names()}"
    assert all("Note" not in r["opener"] for r in reveals), f"a mutating opener must be excluded: {reveals}"
    assert restored, "the probe must restore to baseline after a blocked mutating opener (gate #2)"
    _set_drift()
