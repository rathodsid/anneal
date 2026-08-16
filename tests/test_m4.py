"""M4 (marker: m4) — SCALE. Proves HLD §8/§10/§11 against the golden app, DETERMINISTICALLY (no LLM):

- the exclusive advisory lock (3.1c) serializes exclusive tests per target (disjoint run intervals);
- the suite-end sweeper (§10.2, A16) removes token-bearing residue a run leaves, obeying D15;
- ENV heuristics park a degraded target distinctly — ENV_CAPACITY (429) and ENV_INFRA (5xx) — before
  any test runs, never misread as auth or product failure;
- change-driven selection (§11, A11) scores each test (semantic 0.6 + IDF-structural 0.4, threshold
  0.45), includes unmapped tests cold-start, and explains every pick.
"""
from __future__ import annotations

import json
import urllib.request

import pytest
from sqlalchemy import text

from anneal_artifacts.store import LocalDiskStore
from anneal_classify.selection import select as select_tests
from anneal_core.bundle import Bundle
from anneal_db import repositories as repo
from anneal_db.engine import get_engine
from anneal_scheduler.scheduler import run_suite

from conftest import GOLDEN_URL

_TABLES = ["step_event", "incident_member", "run_result", "run_task", "test_version", "test_golden", "suite_run",
           "suite", "changelog", "incident", "test", "module_version", "module", "session",
           "credential", "territory_notes", "target", "project"]


def _reset_golden():
    req = urllib.request.Request(f"{GOLDEN_URL}/__reset", data=b"{}",
                                 headers={"content-type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()


def _campaign_names():
    data = json.loads(urllib.request.urlopen(f"{GOLDEN_URL}/api/v2/campaigns").read().decode())
    return [c.get("name", "") for c in data.values()]


def _bundle(test_key, steps, nl, *, mutating=False, exclusive=False):
    b = {"schema_version": 1, "project_id": "golden", "test_id": test_key, "version": 1,
         "state": "frozen", "persona": "member", "params": {},
         "lineage": {"parent": None, "reason": "authored", "ruling": "canonical", "incident": None,
                     "triggered_by": "m4_seed", "verified_against": "golden-local"},
         "intent": {"sources": [{"kind": "nl", "verbatim": nl}], "descriptor": nl[:70]},
         "policy": {"exclusive": exclusive, "mutating": mutating}, "steps": steps}
    Bundle.model_validate(b)
    return b


def _goto(idx="s1"):
    return {"id": idx, "intent": "open campaigns", "action": "goto", "arg": "/campaigns",
            "postcondition": {"all": [{"kind": "element", "state": "visible",
                "target": {"strategy": "testid", "selector": "campaigns-table",
                           "fallbacks": {"role": "table"}}}], "budget_ms": 10000}}


def _assert(idx, sel, within=None):
    a = {"state": "visible", "target": {"strategy": "text", "selector": sel}}
    if within:
        a["target"]["within"] = within
    return {"id": idx, "intent": f"see {sel}", "action": "assert", "assert": a,
            "postcondition": None, "reload_on_miss": True, "budget_ms": 8000}


def _readonly(test_key, *, exclusive=False):
    # a padded read-only flow (goto + a few asserts) — enough wall-clock to make interval overlap meaningful
    steps = [_goto(), _assert("s2", "Campaigns", {"role": "navigation"}),
             _assert("s3", "Welcome Campaign", {"testid": "campaigns-table"})]
    return _bundle(test_key, steps, f"smoke {test_key}", exclusive=exclusive)


def _create_only(test_key):
    steps = [
        _goto(),
        {"id": "s2", "intent": "open the create form", "action": "click",
         "target": {"strategy": "testid", "selector": "btn-add-campaign",
                    "fallbacks": {"role": "button", "text": "Add Campaign"}},
         "postcondition": {"all": [{"kind": "element", "state": "visible",
             "target": {"strategy": "testid", "selector": "input-campaign-name"}}], "budget_ms": 8000}},
        {"id": "s3", "intent": "name the campaign", "action": "fill",
         "target": {"strategy": "testid", "selector": "input-campaign-name"},
         "arg": "M4 {{run.token}}", "postcondition": None, "budget_ms": 8000},
        {"id": "s4", "intent": "submit", "action": "click",
         "target": {"strategy": "testid", "selector": "btn-campaign-create-submit",
                    "fallbacks": {"role": "button", "text": "Create"}},
         # arm the create endpoint so it lands in the network log the sweeper reads (as authored bundles do)
         "postcondition": {"all": [{"kind": "network", "method": "POST", "path": "/api/v2/campaigns",
                                    "status": "2xx"},
                                   {"kind": "url", "match": ".*/campaign-v2/[^/]+"}], "budget_ms": 8000}},
    ]
    return _bundle(test_key, steps, "create a campaign (no delete — leaves residue)", mutating=True)


@pytest.fixture
def env():
    eng = get_engine()
    with eng.begin() as c:
        for t in _TABLES:
            c.execute(text(f"delete from {t}"))
    tid = repo.seed_project_target(eng, project_id="golden", project_name="Golden",
                                   target_key="golden-local", base_url=GOLDEN_URL, timezone_="UTC",
                                   profile={"probe_path": "/campaigns"})
    _reset_golden()
    yield {"engine": eng, "target_id": tid}
    _reset_golden()


def _seed(eng, bundle):
    tid, _ = repo.upsert_test(eng, project_id="golden", test_key=bundle["test_id"], bundle=bundle,
                              verified_against="golden-local")
    return tid


def _run(eng, target_id, suite_id, max_parallel=4):
    import asyncio
    art = LocalDiskStore("/tmp/anneal-m4art")
    sr = repo.create_suite_run(eng, project_id="golden", suite_id=suite_id, target_id=target_id)
    return sr, asyncio.run(run_suite(eng, art, suite_run_id=sr, target_id=target_id, max_parallel=max_parallel))


# ═══════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.m4
def test_exclusive_advisory_lock_serializes(env):
    """Two exclusive tests never overlap on a target (3.1c) — the lock is released only after a run
    finishes, so the next exclusive run can only start afterwards. Both still go green (no deadlock)."""
    eng, tid = env["engine"], env["target_id"]
    a = _seed(eng, _readonly("excl-a", exclusive=True))
    b = _seed(eng, _readonly("excl-b", exclusive=True))
    suite = repo.create_suite(eng, project_id="golden", name="excl", member_test_ids=[a, b])
    sr, report = _run(eng, tid, suite, max_parallel=2)
    assert report.verdict in ("green", "green_with_heals"), report

    with eng.connect() as c:
        rows = c.execute(text("""select rr.test_id, rr.started_at, rr.finished_at from run_result rr
                                 join run_task rt on rt.id = rr.run_task_id where rt.suite_run_id=:s
                                 order by rr.started_at"""), {"s": sr}).mappings().all()
    assert len(rows) == 2, rows
    first, second = rows
    assert first["finished_at"] <= second["started_at"], \
        f"exclusive runs overlapped: {first['finished_at']} > {second['started_at']}"


@pytest.mark.m4
def test_advisory_lock_primitive_blocks_second_holder(env):
    """The lock primitive itself: a second acquirer cannot take a target's exclusive lock until the
    first releases (the serialization the scheduler relies on)."""
    eng = env["engine"]
    c1, c2 = eng.connect(), eng.connect()
    try:
        got1 = c1.execute(text("select pg_try_advisory_lock(hashtextextended('golden-local:exclusive',0))")).scalar()
        assert got1 is True
        got2 = c2.execute(text("select pg_try_advisory_lock(hashtextextended('golden-local:exclusive',0))")).scalar()
        assert got2 is False, "a second holder took the lock while it was held"
        c1.execute(text("select pg_advisory_unlock(hashtextextended('golden-local:exclusive',0))"))
        c1.commit()
        got3 = c2.execute(text("select pg_try_advisory_lock(hashtextextended('golden-local:exclusive',0))")).scalar()
        assert got3 is True, "the lock did not free after release"
        c2.execute(text("select pg_advisory_unlock(hashtextextended('golden-local:exclusive',0))"))
        c2.commit()
    finally:
        c1.close(); c2.close()


@pytest.mark.m4
def test_suite_end_sweeper_removes_token_residue(env):
    """A create-only run leaves a token-bearing campaign; the deterministic suite-end sweeper (§10.2)
    derives the collection from the run's OWN network log and DELETEs exactly that entity (D15)."""
    eng, tid = env["engine"], env["target_id"]
    t = _seed(eng, _create_only("residue"))
    suite = repo.create_suite(eng, project_id="golden", name="sweep", member_test_ids=[t])
    sr, report = _run(eng, tid, suite, max_parallel=1)
    assert report.verdict in ("green", "green_with_heals"), report

    with eng.connect() as c:
        token = c.execute(text("select token from run_task where suite_run_id=:s"), {"s": sr}).scalar()
    residue = [n for n in _campaign_names() if token and token in n]
    assert residue == [], f"sweeper left token-bearing residue: {residue}"


@pytest.mark.m4
@pytest.mark.parametrize("probe_path,expect_reason", [
    ("/capacity/campaigns", "ENV_CAPACITY"),
    ("/infra/campaigns", "ENV_INFRA"),
    ("/expired/campaigns", "AUTH_EXPIRED"),
])
def test_env_probe_parks_before_any_run(env, probe_path, expect_reason):
    """A degraded target parks distinctly at the probe — capacity/infra/auth — before any test runs."""
    eng = env["engine"]
    tid = repo.seed_project_target(eng, project_id="golden", project_name="Golden",
                                   target_key=f"golden-{expect_reason.lower()}", base_url=GOLDEN_URL,
                                   timezone_="UTC", profile={"probe_path": probe_path})
    t = _seed(eng, _readonly("probe-victim"))
    suite = repo.create_suite(eng, project_id="golden", name=f"env-{expect_reason}", member_test_ids=[t])
    _, report = _run(eng, tid, suite, max_parallel=1)
    assert report.state == "parked" and report.park_reason == expect_reason, report
    assert report.results == [], "a run executed despite the environment being parked"


@pytest.mark.m4
def test_change_driven_selection_scores_and_explains(env):
    """§11/A11: an NL change selects the tests it touches (semantic + IDF-structural, threshold 0.45),
    includes unmapped tests cold-start, and gives a human-readable reason for every pick."""
    eng = env["engine"]
    _seed(eng, _create_only("campaign-crud"))                    # touches /campaigns + /api/v2/campaigns
    _seed(eng, _bundle("settings-toggle",
                       [{"id": "s1", "intent": "open settings", "action": "goto", "arg": "/settings",
                         "postcondition": {"all": [{"kind": "url", "match": ".*/settings"}], "budget_ms": 8000}}],
                       "toggle a project setting on the settings page"))
    _seed(eng, _bundle("login-smoke",                            # no surfaces → cold-start
                       [{"id": "s1", "intent": "confirm shell", "action": "assert",
                         "assert": {"state": "visible", "target": {"strategy": "testid", "selector": "nav-authenticated"}},
                         "postcondition": None, "budget_ms": 8000}],
                       "confirm the authenticated shell renders"))

    picks = select_tests(eng, project_id="golden", change_nl=(
        "The campaign create form now requires a start date; the POST to the campaigns endpoint 422s "
        "when it is missing."))
    by_key = {p["test_key"]: p for p in picks}
    assert len(picks) == 3 and all(p.get("reason") for p in picks), picks
    assert by_key["campaign-crud"]["selected"] is True, by_key["campaign-crud"]
    assert by_key["campaign-crud"]["score"] > by_key["settings-toggle"]["score"], by_key
    assert by_key["settings-toggle"]["selected"] is False, by_key["settings-toggle"]
    # the unmapped test is included cold-start (safety), with a reason that says so
    assert by_key["login-smoke"]["cold_start"] is True and by_key["login-smoke"]["selected"] is True
    assert "cold-start" in by_key["login-smoke"]["reason"]
