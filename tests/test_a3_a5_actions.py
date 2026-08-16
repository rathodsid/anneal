"""A3–A5 acceptance gates (docs/BACKEND-SPEC.md §Wave A).

A3 == list controls: pagination + filters on runs/incidents/changelog.
A4 == suite CRUD (create/patch membership) + reviewer pin/unpin.
A5 == per-member incident ruling: one atomic POST, {test_id: feature|bug} each, derived incident
ruling (mixed), per-member transitions (feature → provisional canonical; bug → rollback), 409 re-rule.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from anneal_api.app import create_app
from anneal_db import repositories as repo
from anneal_db.engine import get_engine


def _seed_two_tests(engine, key: str) -> dict:
    target_id = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                         target_key=key, base_url="http://localhost:1")
    out = {"target_id": target_id, "tests": {}}
    for name in ("alpha", "beta"):
        tid, _ = repo.upsert_test(engine, project_id="golden", test_key=f"{key}-{name}",
                                  bundle={"persona": "member"}, verified_against=key)
        out["tests"][name] = tid
    return out


@pytest.mark.api_contract
def test_a3_list_controls():
    engine = get_engine()
    ids = _seed_two_tests(engine, "a3-controls")
    for name in ("alpha", "beta"):
        repo.changelog_add(engine, project_id="golden", kind="heal",
                           summary=f"a3 {name}", test_id=ids["tests"][name])
    with TestClient(create_app(engine=engine)) as tc:
        # changelog: test_id + kind filters, pagination
        rows = tc.get("/projects/golden/changelog",
                      params={"test_id": ids["tests"]["alpha"], "kind": "heal"}).json()
        assert rows and all(r["test_id"] == ids["tests"]["alpha"] for r in rows)
        assert len(tc.get("/projects/golden/changelog", params={"limit": 1}).json()) == 1
        page2 = tc.get("/projects/golden/changelog", params={"limit": 1, "offset": 1}).json()
        assert len(page2) == 1 and page2 != tc.get("/projects/golden/changelog",
                                                   params={"limit": 1}).json()
        # runs: verdict filter returns only matches; bogus verdict returns none
        assert all(r["verdict"] == "green"
                   for r in tc.get("/projects/golden/runs", params={"verdict": "green"}).json())
        assert tc.get("/projects/golden/runs", params={"verdict": "no-such"}).json() == []
        # incidents: klass filter + pagination params accepted
        assert isinstance(tc.get("/projects/golden/incidents",
                                 params={"klass": "behavior", "limit": 5}).json(), list)


@pytest.mark.api_contract
def test_a4_suite_crud_and_pin():
    # unique key per invocation — the dev DB persists across runs; residue must never fail the gate
    key = f"a4-crud-{uuid.uuid4().hex[:6]}"
    engine = get_engine()
    ids = _seed_two_tests(engine, key)
    t1, t2 = ids["tests"]["alpha"], ids["tests"]["beta"]
    with TestClient(create_app(engine=engine)) as tc:
        s = tc.post("/projects/golden/suites",
                    json={"name": f"{key}-suite", "member_test_ids": [t1]}).json()
        assert s["name"] == f"{key}-suite" and s["member_test_ids"] == [t1]
        s2 = tc.patch(f"/suites/{s['id']}", json={"member_test_ids": [t1, t2]}).json()
        assert s2["member_test_ids"] == [t1, t2]
        s3 = tc.patch(f"/suites/{s['id']}", json={"name": f"{key}-renamed"}).json()
        assert s3["name"] == f"{key}-renamed" and s3["member_test_ids"] == [t1, t2]  # untouched
        assert tc.patch(f"/suites/{t1}", json={"name": "x"}).status_code == 404  # not a suite id

        # pin/unpin round-trip on a real run
        suite = repo.create_suite(engine, project_id="golden", name=f"{key}-run", member_test_ids=[t1])
        sr = repo.create_suite_run(engine, project_id="golden", suite_id=suite,
                                   target_id=ids["target_id"])
        repo.expand_run_tasks(engine, suite_run_id=sr)
        claim = repo.claim_next(engine, suite_run_id=sr, worker_id="a4")
        rr = repo.start_run_result(engine, run_task_id=claim["run_task_id"], test_id=t1,
                                   test_version=1, environment="a4-crud")
        repo.finish_run_result(engine, run_result_id=rr, run_task_id=claim["run_task_id"],
                               test_id=t1, verdict="green")
        repo.set_suite_run_state(engine, suite_run_id=sr, state="done", verdict="green")  # B1 lock
        assert tc.post(f"/runs/{rr}/pin", json={"pinned": False}).json()["pinned"] is False
        assert tc.post(f"/runs/{rr}/pin", json={"pinned": True}).json()["pinned"] is True


@pytest.mark.api_contract
def test_a5_per_member_ruling_mixed():
    key = f"a5-ruling-{uuid.uuid4().hex[:6]}"        # unique per invocation (persistent dev DB)
    engine = get_engine()
    ids = _seed_two_tests(engine, key)
    t1, t2 = ids["tests"]["alpha"], ids["tests"]["beta"]
    target = repo.get_target(engine, target_id=ids["target_id"])
    from anneal_classify import incidents as inc_machine
    iid, created = inc_machine.attach(engine, project_id="golden", klass="behavior", target=target,
                                      step_sig=f"{key}-step", error_class="POSTCONDITION_FAILED",
                                      test_id=t1, red_run_id=None)
    assert created
    inc_machine.attach(engine, project_id="golden", klass="behavior", target=target,
                       step_sig=f"{key}-step", error_class="POSTCONDITION_FAILED",
                       test_id=t2, red_run_id=None)
    for tid in (t1, t2):  # each member got a provisional re-author under this incident
        repo.create_test_version(engine, test_id=tid, bundle={"persona": "member", "v": 2},
                                 reason="reauthor", parent_version=1, verified_against=key,
                                 state="provisional", incident_id=iid, apply=True)

    with TestClient(create_app(engine=engine)) as tc:
        # neither shape → 422; both shapes → 422 (model-enforced exactly-one)
        assert tc.post(f"/incidents/{iid}/ruling", json={}).status_code == 422
        assert tc.post(f"/incidents/{iid}/ruling",
                       json={"ruling": "feature", "members": {t1: "bug"}}).status_code == 422
        # unknown member → 409
        assert tc.post(f"/incidents/{iid}/ruling",
                       json={"members": {ids["target_id"]: "bug"}}).status_code == 409

        r = tc.post(f"/incidents/{iid}/ruling",
                    json={"members": {t1: "feature", t2: "bug"}, "ruling_note": "half real"})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ruling"] == "mixed" and set(out["affected"]) == {f"{key}-alpha", f"{key}-beta"}
        assert out["members"] == {t1: "feature", t2: "bug"}

        # feature member: provisional v2 is canonical; bug member: rolled back to v1
        assert repo.get_test(engine, test_id=t1)["head_version"] == 2
        assert repo.get_test_version(engine, test_id=t1, version=2)["state"] == "frozen"
        assert repo.get_test(engine, test_id=t2)["head_version"] == 1
        assert repo.get_test_version(engine, test_id=t2, version=2)["state"] == "rolled_back"

        # per-member decisions recorded; incident closed + derived ruling; re-rule → 409
        detail = tc.get(f"/incidents/{iid}").json()
        assert detail["state"] == "closed" and detail["ruling"] == "mixed"
        assert {m["test_id"]: m["ruling"] for m in detail["members"]} == {t1: "feature", t2: "bug"}
        assert tc.post(f"/incidents/{iid}/ruling",
                       json={"members": {t1: "feature"}}).status_code == 409
