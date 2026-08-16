"""M1 (marker: m1) — cold lane + console v0. Proves each HLD §15 exit criterion end-to-end:

- a read-only suite runs in PARALLEL, green, each test in seconds;
- an expired session PARKS the suite AUTH_EXPIRED before any run_task runs;
- the console/API expose run detail as NL intent ↔ frames ↔ network (D33);
- CLI run/status/export work against the one API (D32);
- retention prune keeps the pinned latest run per test (D12).

These tests seed the dev postgres (brought up by verify.sh) and drive the hermetic golden app.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from anneal_artifacts.store import LocalDiskStore
from anneal_core.jsoncutil import loads as jsonc_loads
from anneal_db import repositories as repo
from anneal_db.engine import get_engine

from _dbguard import assert_wipeable_db
from conftest import GOLDEN_URL

ROOT = Path(__file__).resolve().parents[1]
# FK-safe order (children before parents; incident_member.red_run_id→run_result, added in M3)
_TABLES = ["step_event", "incident_member", "run_result", "run_task", "test_version", "test_golden",
           "suite_run", "suite", "changelog", "incident", "test", "module_version", "module", "session",
           "credential", "territory_notes", "target", "project"]


def _wipe(engine):
    assert_wipeable_db()          # refuse to delete rows unless this is a disposable test DB
    with engine.begin() as c:
        for t in _TABLES:
            c.execute(text(f"delete from {t}"))


def seed(engine) -> dict:
    """Wipe + seed: golden target, an 'expired' target, the login module, the read-only tests + suite,
    the campaign-crud test + suite (for the network demo), and an expired-session suite."""
    _wipe(engine)
    # probe_path is a TargetProfile fact (§9/D25), not hardcoded in the engine: the golden app's
    # authenticated landing is /campaigns; for the 'expired' target base_url+/campaigns serves login.
    prof = {"probe_path": "/campaigns"}
    tid = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                   target_key="golden-local", base_url=GOLDEN_URL, timezone_="UTC",
                                   profile=prof)
    exp_tid = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                       target_key="golden-expired", base_url=GOLDEN_URL + "/expired",
                                       timezone_="UTC", profile=prof)
    login = json.loads((ROOT / "fixtures" / "modules" / "login.v4.json").read_text())
    repo.upsert_module(engine, project_id="golden", ref="login", version=4, steps=login["steps"])

    tests = {}
    for f in ["fixtures/m1/campaigns-list-smoke.bundle.json", "fixtures/m1/login-smoke.bundle.json"]:
        b = json.loads((ROOT / f).read_text())
        t_id, _ = repo.upsert_test(engine, project_id="golden", test_key=b["test_id"], bundle=b,
                                   verified_against="golden-local")
        tests[b["test_id"]] = t_id
    crud = jsonc_loads((ROOT / "fixtures" / "campaign-crud.bundle.jsonc").read_text())
    crud_id, _ = repo.upsert_test(engine, project_id="golden", test_key="campaign-crud", bundle=crud,
                                  verified_against="golden-local")
    tests["campaign-crud"] = crud_id

    # Label vocabulary is DATA, never engine code (rule 2) — the golden project ships pre-labelled so
    # the catalog's label axis is non-empty in dev. Additive: no assertion depends on these.
    for key, labels in {"campaigns-list-smoke": ["smoke", "regression"],
                        "login-smoke": ["smoke", "sanity"],
                        "campaign-crud": ["regression"]}.items():
        repo.set_test_labels(engine, test_id=tests[key], labels=labels)

    readonly = repo.create_suite(engine, project_id="golden", name="m1-readonly",
                                 member_test_ids=[tests["campaigns-list-smoke"], tests["login-smoke"]])
    crud_suite = repo.create_suite(engine, project_id="golden", name="m1-crud",
                                   member_test_ids=[crud_id])
    expired_suite = repo.create_suite(engine, project_id="golden", name="m1-expired",
                                      member_test_ids=[tests["login-smoke"]])
    return {"target_id": tid, "expired_target_id": exp_tid, "readonly_suite": readonly,
            "crud_suite": crud_suite, "expired_suite": expired_suite, "tests": tests}


@pytest.fixture
def engine():
    return get_engine()


@pytest.fixture(autouse=True)
def _isolated_db():
    """Per-test DB isolation: wipe (FK-safe order — children first) before EVERY m1 test, so a prior
    test's red-run incident/member residue can never cascade into another test (e.g. the retention-prune
    assertions). seed() also wipes, but this makes isolation the default and covers tests that don't."""
    _wipe(get_engine())
    yield


@pytest.fixture
def artifacts(tmp_path):
    return LocalDiskStore(str(tmp_path / "artifacts"))


# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.m1
async def test_readonly_suite_runs_parallel_green_in_seconds(engine, artifacts):
    from anneal_scheduler.scheduler import run_suite
    ids = seed(engine)
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=ids["readonly_suite"],
                               target_id=ids["target_id"])
    report = await run_suite(engine, artifacts, suite_run_id=sr, target_id=ids["target_id"],
                             max_parallel=2)
    assert report.state == "done" and report.verdict == "green", report
    assert len(report.results) == 2
    for r in report.results:
        assert r["verdict"] == "green"
        assert r["duration_ms"] < 30000, "cold replay must be in seconds (<30s, baseline speed gate)"
        steps = repo.get_steps(engine, run_result_id=r["run_result_id"])
        assert steps and steps[-1]["status"] == "passed"
    # the two runs actually ran CONCURRENTLY (not just both green): their [start,finish] intervals
    # overlap on the wall clock — with 2 workers each run interleaves at every browser await.
    with engine.connect() as c:
        iv = c.execute(text("select started_at, finished_at from run_result order by started_at")).all()
    assert len(iv) == 2 and iv[0].started_at < iv[1].finished_at and iv[1].started_at < iv[0].finished_at, \
        "the two runs did not overlap — concurrency is not real"


@pytest.mark.m1
async def test_auth_expired_parks_before_any_run(engine, artifacts):
    from anneal_scheduler.scheduler import run_suite
    ids = seed(engine)
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=ids["expired_suite"],
                               target_id=ids["expired_target_id"])
    report = await run_suite(engine, artifacts, suite_run_id=sr, target_id=ids["expired_target_id"])
    assert report.state == "parked"
    assert report.verdict == "parked_auth" and report.park_reason == "AUTH_EXPIRED"
    # the amplification trap is closed: NOTHING ran — zero run_results, queued tasks cancelled.
    with engine.connect() as c:
        n_results = c.execute(text("select count(*) from run_result rr join run_task t on t.id=rr.run_task_id "
                                   "where t.suite_run_id=:s"), {"s": sr}).scalar()
        n_queued = c.execute(text("select count(*) from run_task where suite_run_id=:s and state='queued'"),
                             {"s": sr}).scalar()
        n_cancelled = c.execute(text("select count(*) from run_task where suite_run_id=:s and state='cancelled'"),
                                {"s": sr}).scalar()
    assert n_results == 0, "a test ran despite an expired session (amplification trap)"
    assert n_queued == 0 and n_cancelled >= 1


@pytest.mark.m1
async def test_api_run_detail_exposes_intent_frames_network(engine, artifacts):
    from httpx import ASGITransport, AsyncClient

    from anneal_api.app import create_app
    ids = seed(engine)
    app = create_app(engine=engine, artifacts=artifacts)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # launch the mutating campaign-crud flow (self-cleaning) so network events are populated.
        # B1 (approved 2026-07-16): launch is async — 202 + suite_run_id immediately; poll the
        # suite_run to terminal, then make the SAME verdict/results assertions as before.
        resp = await client.post(f"/projects/golden/suites/{ids['crud_suite']}/runs",
                                 params={"target_id": ids["target_id"]})
        assert resp.status_code == 202, resp.text
        sr_id = resp.json()["suite_run_id"]
        for _ in range(600):                      # ≤120s — the crud flow normally finishes in ~20s
            launched = (await client.get(f"/suite_runs/{sr_id}")).json()
            if launched["state"] in ("done", "parked"):
                break
            await asyncio.sleep(0.2)
        assert launched["state"] == "done", launched
        assert launched["verdict"] == "green", launched
        rr_id = launched["results"][0]["id"]

        runs = (await client.get("/projects/golden/runs")).json()
        assert any(r["id"] == rr_id and r["pinned"] for r in runs)

        detail = (await client.get(f"/runs/{rr_id}")).json()
        assert detail["intent_nl"] and "campaign" in detail["intent_nl"].lower()   # NL intent
        assert detail["steps"] and all("network" in s for s in detail["steps"])    # frames w/ network field
        net = [n for s in detail["steps"] for n in s["network"]]
        methods = {n["method"] for n in net}
        assert "POST" in methods and "DELETE" in methods, f"network not surfaced: {net}"


@pytest.mark.m1
def test_cli_run_status_export(engine, tmp_path):
    """The CLI drives the same API a CI job calls (D32): run → status → export, via a live server."""
    import uvicorn

    from anneal_api.app import create_app
    from anneal_cli.main import main as cli
    ids = seed(engine)
    app = create_app(engine=engine, artifacts=LocalDiskStore(str(tmp_path / "cliart")))

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                httpx.get(base + "/openapi.json", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        client = httpx.Client(base_url=base, timeout=180)
        try:
            # run (launch the read-only suite) → exit 0 (green)
            rc = cli(["run", "golden", ids["readonly_suite"], "--target", ids["target_id"]], client=client)
            assert rc == 0
            # status of the latest run
            runs = client.get("/projects/golden/runs").json()
            assert runs, "no runs after CLI run"
            rc = cli(["status", runs[0]["id"]], client=client)
            assert rc == 0
            # export a compilable standalone script
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli(["export", ids["tests"]["campaigns-list-smoke"], "1"], client=client)
            assert rc == 0
            compile(buf.getvalue(), "exported.py", "exec")          # runnable script (the #1/#2 M0 lesson)
        finally:
            client.close()
    finally:
        server.should_exit = True
        th.join(timeout=10)


@pytest.mark.m1
async def test_retention_prune_7day_window_pinned_exempt(engine, artifacts):
    """D12: prune deletes run history older than 7 days, but keeps (a) any run younger than the window
    and (b) the pinned latest run per test regardless of age."""
    from anneal_scheduler.scheduler import run_suite
    ids = seed(engine)
    # run the same read-only suite TWICE: 4 runs (2 tests × 2), the newest per test pinned (D12)
    for _ in range(2):
        sr = repo.create_suite_run(engine, project_id="golden", suite_id=ids["readonly_suite"],
                                   target_id=ids["target_id"])
        await run_suite(engine, artifacts, suite_run_id=sr, target_id=ids["target_id"], max_parallel=2)

    with engine.connect() as c:
        before = c.execute(text("select count(*) from run_result")).scalar()
        pinned = c.execute(text("select count(*) from run_result where pinned")).scalar()
    assert before == 4 and pinned == 2, "expected 4 runs (2 tests × 2), 2 pinned latest"

    # 1) all four runs are YOUNG (just now) → prune deletes NOTHING (7-day window protects them).
    assert repo.prune_runs(engine, keep_pinned=True) == 0, "prune deleted a run younger than 7 days"
    with engine.connect() as c:
        assert c.execute(text("select count(*) from run_result")).scalar() == 4

    # 2) age the NON-pinned runs past the window; the pinned newest stays young.
    with engine.begin() as c:
        c.execute(text("update run_result set finished_at = now() - interval '8 days' where pinned = false"))
        newest = {row.test_id: str(row.id) for row in c.execute(text(
            "select distinct on (test_id) test_id, id from run_result order by test_id, started_at desc"))}
    deleted = repo.prune_runs(engine, keep_pinned=True)
    assert deleted == 2, "prune should delete exactly the two aged non-pinned runs"
    with engine.connect() as c:
        survivors = {str(r.id) for r in c.execute(text("select id from run_result"))}
        orphan_events = c.execute(text(
            "select count(*) from step_event se left join run_result rr on rr.id=se.run_result_id "
            "where rr.id is null")).scalar()
    assert survivors == set(newest.values()), "prune did not keep exactly the newest (pinned) run per test"
    assert orphan_events == 0, "prune left orphaned step_events"


@pytest.mark.m1
def test_prune_respects_incident_referenced_runs(engine):
    """D12 × incident lifecycle (fk_member_run): a run that OPENED an incident must not be pruned while
    the incident is unresolved (state <> 'closed') — it's the evidence the inbox shows; and once the
    incident is CLOSED, the aged-out run prunes with its incident_member.red_run_id NULLed (the metadata
    outlives the artifact) and NO ForeignKeyViolation. Exercises the exact FK path prune crashed on."""
    ids = seed(engine)
    tid = ids["target_id"]
    # a real RED run_result for a seeded test
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=ids["expired_suite"], target_id=tid)
    repo.expand_run_tasks(engine, suite_run_id=sr)
    claim = repo.claim_next(engine, suite_run_id=sr, worker_id="w0")
    rr = repo.start_run_result(engine, run_task_id=claim["run_task_id"], test_id=claim["test_id"],
                               test_version=claim["test_version"], environment="golden-local")
    repo.finish_run_result(engine, run_result_id=rr, run_task_id=claim["run_task_id"],
                           test_id=claim["test_id"], verdict="red", error_class="POSTCONDITION_UNMET",
                           duration_ms=100)
    # an incident opened by that red run → incident_member.red_run_id = rr (the FK prune must respect)
    iid, _ = repo.attach_incident(engine, project_id="golden", klass="behavior", surface="/campaigns",
                                  step_sig="|click|testid|x|", error_class="POSTCONDITION_UNMET",
                                  cap_agent_runs=8, cap_llm_calls=200, test_id=claim["test_id"], red_run_id=rr)
    # age it past the window and un-pin it, so ONLY the incident-exemption can protect it
    with engine.begin() as c:
        c.execute(text("update run_result set finished_at = now() - interval '8 days', pinned=false "
                       "where id=:r"), {"r": rr})

    # 1) OPEN incident → the run is exempt from prune (live evidence); NO FK error, run survives
    repo.prune_runs(engine, keep_pinned=True)
    with engine.connect() as c:
        assert c.execute(text("select 1 from run_result where id=:r"), {"r": rr}).first() is not None, \
            "prune deleted a run referenced by an OPEN incident (lost the inbox's evidence)"

    # 2) CLOSE the incident → the aged run prunes, its member ref NULLed, NO ForeignKeyViolation
    repo.set_incident(engine, incident_id=iid, state="closed", closed=True)
    repo.prune_runs(engine, keep_pinned=True)
    with engine.connect() as c:
        assert c.execute(text("select 1 from run_result where id=:r"), {"r": rr}).first() is None, \
            "closed-incident aged run was not pruned (D12)"
        assert c.execute(text("select red_run_id from incident_member where incident_id=:i"),
                         {"i": iid}).scalar() is None, "red_run_id not NULLed → prune would have FK-violated"
        assert c.execute(text("select count(*) from incident_member where incident_id=:i"),
                         {"i": iid}).scalar() == 1, "incident_member metadata should outlive the pruned run"


@pytest.mark.m1
def test_probe_injects_seeded_storage_state(engine, tmp_path):
    """The §9 probe validates the SEEDED session, not an anonymous visit: the scheduler looks up
    session.storage_state_path for the bound persona and injects it. Prove the lookup + persona wiring
    (full auth-target injection is exercised live in M2)."""
    ids = seed(engine)
    # no session seeded yet → lookup is None (hermetic golden app needs none)
    assert repo.get_storage_state(engine, project_id="golden", persona="member") is None
    # the ref must point at a file that EXISTS on disk: get_storage_state degrades a dangling path to None
    # (a reset/pruned artifacts volume must never hand a dead path to browser.new_context), so seed a real
    # materialized storage-state file to prove the lookup + persona wiring.
    ss = tmp_path / "member.json"
    ss.write_text("{}")
    # seed a credential + valid session and confirm the scheduler's lookup finds its storage-state
    with engine.begin() as c:
        cred = c.execute(text("insert into credential (project_id, persona, secret_enc) "
                              "values ('golden','member','\\x00') returning id")).one().id
        # session is now keyed (credential_id, target_id) — the seed must name the target (0003).
        c.execute(text("insert into session (credential_id, target_id, storage_state_path, state) "
                       "values (:c, :t, :ss, 'valid')"),
                  {"c": cred, "t": ids["target_id"], "ss": str(ss)})
    assert repo.get_storage_state(engine, project_id="golden", persona="member") == str(ss)
    # the suite's members bind the 'member' persona the probe checks
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=ids["readonly_suite"],
                               target_id=ids["target_id"])
    assert repo.suite_member_personas(engine, suite_run_id=sr) == ["member"]


@pytest.mark.m1
def test_launch_rejects_non_d21_trigger(engine):
    from fastapi.testclient import TestClient

    from anneal_api.app import create_app
    ids = seed(engine)
    with TestClient(create_app(engine=engine)) as tc:
        r = tc.post(f"/projects/golden/suites/{ids['readonly_suite']}/runs",
                    params={"target_id": ids["target_id"], "trigger": "cron"})
    assert r.status_code == 422, "D21: cron is not a valid trigger"


@pytest.mark.m1
def test_console_is_served(engine):
    from httpx import Client

    from fastapi.testclient import TestClient

    from anneal_api.app import create_app
    app = create_app(engine=engine)
    with TestClient(app) as tc:
        html = tc.get("/").text
        assert "Anneal" in html and "runs" in html.lower()


@pytest.mark.m1
def test_finalize_never_greens_over_incomplete_evidence_trail():
    """D33 integrity (pure): a PASS-class verdict must not finalize when fewer step_events were
    persisted than steps executed — the evidence trail classification+console read would be
    incomplete, so it downgrades to a loud engine fault instead of a silent green. A real red stands."""
    from anneal_scheduler.scheduler import _finalize_with_evidence
    assert _finalize_with_evidence("green", None, 3, 3) == ("green", None)          # complete → unchanged
    assert _finalize_with_evidence("green_with_heals", None, 5, 5) == ("green_with_heals", None)
    assert _finalize_with_evidence("green", None, 0, 3) == ("red", "EVIDENCE_INCOMPLETE")   # empty trail
    assert _finalize_with_evidence("provisional", None, 2, 3) == ("red", "EVIDENCE_INCOMPLETE")  # short
    assert _finalize_with_evidence("red", "LOCATOR_DRIFT", 0, 3) == ("red", "LOCATOR_DRIFT")  # red untouched


@pytest.mark.m1
async def test_verdict_awaits_durable_step_events(engine):
    """D33 integrity (live DB): the read-back guard reports the trail's completeness for a run and
    never blocks forever on a shortfall — so a run's verdict is only finalized once its step_events
    are durably visible (the invariant behind test_readonly_suite's steps[-1]=='passed' assertion)."""
    from anneal_db.sink import DBStepEventSink
    from anneal_scheduler.scheduler import _ensure_step_events_visible

    ids = seed(engine)
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=ids["readonly_suite"],
                               target_id=ids["target_id"])
    repo.expand_run_tasks(engine, suite_run_id=sr)
    claim = repo.claim_next(engine, suite_run_id=sr, worker_id="w0")
    rr = repo.start_run_result(engine, run_task_id=claim["run_task_id"], test_id=claim["test_id"],
                               test_version=claim["test_version"], environment="golden-local")
    sink = DBStepEventSink(engine)
    # a SHORT trail (1 of an expected 3): the guard reports the shortfall and returns (does not hang)
    sink.emit(rr, {"step_index": 0, "step_id": "s1", "action": "goto", "status": "passed"})
    assert await _ensure_step_events_visible(engine, rr, 3) == 1
    assert repo.count_step_events(engine, run_result_id=rr) == 1
    # complete the trail → the guard sees every event, so a pass would be allowed to finalize
    sink.emit(rr, {"step_index": 1, "step_id": "s2", "action": "click", "status": "passed"})
    sink.emit(rr, {"step_index": 2, "step_id": "s3", "action": "assert", "status": "passed"})
    assert await _ensure_step_events_visible(engine, rr, 3) == 3
