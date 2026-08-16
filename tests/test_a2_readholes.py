"""A2 acceptance gate (docs/BACKEND-SPEC.md §Wave A) — every read-hole has a route.

DONE for A2 == the previously DB-only data is fetchable: test catalog + version lineage (with D7
calibration), suites + suite_runs post-hoc, TerritoryNotes, session health, targets, and the enriched
list_runs projection (off_canonical/trigger/…). Seeds through the real repo lifecycle — no browser.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from anneal_api.app import create_app
from anneal_db import repositories as repo
from anneal_db.engine import get_engine


def _seed(engine, *, key="a2-readholes") -> dict:
    target_id = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                         target_key=key, base_url="http://localhost:1")
    test_id, _ = repo.upsert_test(engine, project_id="golden", test_key=f"{key}-test",
                                  bundle={"persona": "member"}, verified_against=key)
    suite = repo.create_suite(engine, project_id="golden", name=key, member_test_ids=[test_id])
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=suite, target_id=target_id,
                               mode="saved", trigger="deploy_hook")
    repo.expand_run_tasks(engine, suite_run_id=sr)
    claim = repo.claim_next(engine, suite_run_id=sr, worker_id="a2")
    rr = repo.start_run_result(engine, run_task_id=claim["run_task_id"], test_id=test_id,
                               test_version=1, environment=key)
    repo.finish_run_result(engine, run_result_id=rr, run_task_id=claim["run_task_id"],
                           test_id=test_id, verdict="green", duration_ms=42)
    repo.set_suite_run_state(engine, suite_run_id=sr, state="done", verdict="green")
    repo.upsert_territory_notes(engine, target_id=target_id,
                                notes={"routes": ["/campaigns"], "idioms": ["combobox"]})
    # a seeded persona session (credential has no repo writer — the seeding flow owns it; raw here)
    with engine.begin() as c:
        c.execute(text("delete from session where credential_id in "
                       "(select id from credential where project_id='golden' and persona='a2-member')"))
        c.execute(text("delete from credential where project_id='golden' and persona='a2-member'"))
        cred = c.execute(text("insert into credential (project_id, persona, secret_enc) "
                              "values ('golden', 'a2-member', '\\x00'::bytea) returning id")).one().id
        c.execute(text("insert into session (credential_id, target_id, storage_state_path, seeded_at, "
                       "last_validated, state) values (:c, :t, '/tmp/x.json', now(), now(), 'valid')"),
                  {"c": cred, "t": target_id})  # session is (credential_id, target_id)-keyed since 0007
    return {"target_id": target_id, "test_id": test_id, "suite": suite, "suite_run": sr, "run": rr}


@pytest.mark.api_contract
def test_readholes_end_to_end():
    engine = get_engine()
    ids = _seed(engine)
    with TestClient(create_app(engine=engine)) as tc:
        # test catalog + detail + lineage + calibration-bearing version
        cat = tc.get("/projects/golden/tests").json()
        assert any(t["test_key"] == "a2-readholes-test" for t in cat)
        assert tc.get(f"/tests/{ids['test_id']}").json()["test_key"] == "a2-readholes-test"
        versions = tc.get(f"/tests/{ids['test_id']}/versions").json()
        assert [v["version"] for v in versions] == [1] and versions[0]["state"] == "frozen"
        v1 = tc.get(f"/tests/{ids['test_id']}/versions/1").json()
        assert v1["bundle"]["persona"] == "member" and "calibration" in v1
        assert tc.get(f"/tests/{ids['test_id']}/versions/99").status_code == 404

        # suites + suite_run post-hoc (B1's poll target)
        suites = tc.get("/projects/golden/suites").json()
        assert any(s["name"] == "a2-readholes" for s in suites)
        assert tc.get(f"/suites/{ids['suite']}").json()["member_test_ids"] == [ids["test_id"]]
        srd = tc.get(f"/suite_runs/{ids['suite_run']}").json()
        assert (srd["state"], srd["verdict"], srd["trigger"], srd["mode"]) == \
               ("done", "green", "deploy_hook", "saved")
        assert srd["results"][0]["id"] == ids["run"] and srd["results"][0]["verdict"] == "green"
        assert tc.get(f"/suite_runs/{uuid.uuid4()}").status_code == 404

        # territory (write existed, read was the hole)
        terr = tc.get(f"/targets/{ids['target_id']}/territory").json()
        assert terr["notes"]["routes"] == ["/campaigns"] and terr["version"] >= 1

        # session health
        sessions = tc.get("/projects/golden/sessions").json()
        mine = [s for s in sessions if s["persona"] == "a2-member"]
        assert mine and mine[0]["state"] == "valid" and mine[0]["seeded_at"]
        assert "storage_state_path" not in mine[0]                    # stays server-side

        # targets
        targets = tc.get("/projects/golden/targets").json()
        assert any(t["key"] == "a2-readholes" for t in targets)

        # enriched runs projection: Runs-list columns present
        runs = tc.get("/projects/golden/runs").json()
        row = next(r for r in runs if r["id"] == ids["run"])
        assert row["trigger"] == "deploy_hook" and row["off_canonical"] is False
        assert row["param_row"] == "default" and row["browser"] == "chromium"
