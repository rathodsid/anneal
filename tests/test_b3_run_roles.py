"""B3 acceptance gates (docs/BACKEND-SPEC.md §Wave B) — retry/heal/verify as real run_results.

DONE for B3 == a non-primary run (retry|heal|verify) is a real, addressable run_result with its OWN
artifact dir and step trail; run history stays clean (list_runs defaults to role='primary'); the
money-shot can reach the verify replay via incident_member.verify_run_id; and D12 prune protects the
verify evidence of a live incident exactly like the red run.

The full-integration proof is the m3 gate: the red path drives retry/heal/verify through these paths
against a real browser and still reaches its exact verdicts + one-incident/one-guided assertions.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from anneal_api.app import create_app
from anneal_db import repositories as repo
from anneal_db.engine import get_engine
from anneal_db.sink import DBStepEventSink


def _seed_red_run(engine, key: str) -> dict:
    target_id = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                         target_key=key, base_url="http://localhost:1")
    test_id, _ = repo.upsert_test(engine, project_id="golden", test_key=f"{key}-test",
                                  bundle={"persona": "member"}, verified_against=key)
    suite = repo.create_suite(engine, project_id="golden", name=key, member_test_ids=[test_id])
    sr = repo.create_suite_run(engine, project_id="golden", suite_id=suite, target_id=target_id)
    repo.expand_run_tasks(engine, suite_run_id=sr)
    claim = repo.claim_next(engine, suite_run_id=sr, worker_id="b3")
    red = repo.start_run_result(engine, run_task_id=claim["run_task_id"], test_id=test_id,
                                test_version=1, environment=key)
    repo.finish_run_result(engine, run_result_id=red, run_task_id=claim["run_task_id"],
                           test_id=test_id, verdict="red", error_class="POSTCONDITION_FAILED")
    repo.set_suite_run_state(engine, suite_run_id=sr, state="done", verdict="red")
    return {"target_id": target_id, "test_id": test_id, "red": red}


@pytest.mark.api_contract
def test_child_run_is_addressable_and_hidden_from_default_history():
    engine = get_engine()
    key = f"b3-roles-{uuid.uuid4().hex[:6]}"
    ids = _seed_red_run(engine, key)

    # a verify child of the red run — real row, inherits task/test/version/env, own step trail
    verify = repo.start_child_run(engine, parent_run_id=ids["red"], role="verify")
    DBStepEventSink(engine).emit(verify, {"step_index": 0, "step_id": "s0", "action": "goto",
                                          "status": "passed"})
    repo.finish_child_run(engine, run_result_id=verify, verdict="green", duration_ms=7)

    detail = repo.get_run(engine, run_result_id=verify)
    assert detail["role"] == "verify" and str(detail["test_id"]) == ids["test_id"]
    assert detail["test_version"] == 1 and detail["environment"] == key   # inherited from the red run

    with TestClient(create_app(engine=engine)) as tc:
        # default history is primary-only — the verify child must NOT appear
        primary = tc.get("/projects/golden/runs", params={"test_id": ids["test_id"]}).json()
        assert [r["id"] for r in primary] == [ids["red"]]
        assert primary[0]["role"] == "primary"

        # role='verify' surfaces it; role='any' shows both
        only_verify = tc.get("/projects/golden/runs",
                             params={"test_id": ids["test_id"], "role": "verify"}).json()
        assert [r["id"] for r in only_verify] == [verify]
        both = tc.get("/projects/golden/runs",
                     params={"test_id": ids["test_id"], "role": "any"}).json()
        assert {r["id"] for r in both} == {ids["red"], verify}

        # the child's own trail + artifacts endpoint are addressable
        assert tc.get(f"/runs/{verify}/steps").json()[0]["status"] == "passed"
        assert tc.get(f"/runs/{verify}/artifacts/network_log").status_code == 200


@pytest.mark.api_contract
def test_incident_member_links_verify_run_and_prune_protects_it():
    engine = get_engine()
    key = f"b3-verify-{uuid.uuid4().hex[:6]}"
    ids = _seed_red_run(engine, key)
    target = repo.get_target(engine, target_id=ids["target_id"])
    from anneal_classify import incidents as inc_machine
    iid, _ = inc_machine.attach(engine, project_id="golden", klass="behavior", target=target,
                                step_sig=f"{key}-step", error_class="POSTCONDITION_FAILED",
                                test_id=ids["test_id"], red_run_id=ids["red"])
    verify = repo.start_child_run(engine, parent_run_id=ids["red"], role="verify")
    repo.finish_child_run(engine, run_result_id=verify, verdict="green", duration_ms=5)
    repo.set_member_verify_run(engine, incident_id=iid, test_id=ids["test_id"], verify_run_id=verify)

    with TestClient(create_app(engine=engine)) as tc:
        members = tc.get(f"/incidents/{iid}").json()["members"]
        m = next(m for m in members if m["test_id"] == ids["test_id"])
        assert m["red_run_id"] == ids["red"] and m["verify_run_id"] == verify

    # D12: the verify replay of a still-open incident is live evidence — prune must NOT delete it
    # (force the age window to 0 days so only the incident-reference clause can protect it)
    deleted = repo.prune_runs(engine, keep_pinned=False, older_than_days=0)
    assert repo.get_run(engine, run_result_id=verify) is not None, \
        f"prune wrongly deleted the verify evidence of a live incident (deleted={deleted})"
