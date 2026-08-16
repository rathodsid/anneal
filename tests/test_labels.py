"""Labels acceptance gates — catalog metadata + running a label as a run.

DONE == a test carries free-form labels (catalog metadata, no version minted), the catalog filters by
one, a label launches as a run whose TARGET IS DERIVED from the members' canonical binding, and every
way that derivation can be ambiguous or empty refuses instead of guessing.

Additive gate — strengthens verify.sh's api_contract lane, weakens nothing. Seeds through the real
repo lifecycle; the one full-replay test uses the hermetic golden app like the M1 lane does.
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from anneal_api.app import create_app
from anneal_db import repositories as repo
from anneal_db.engine import get_engine

from conftest import GOLDEN_URL, ROOT


def _bundle(name: str) -> dict:
    import json
    return json.loads((ROOT / "fixtures" / "m1" / f"{name}.bundle.json").read_text())


def _seed(engine, key: str, *, base_url: str = GOLDEN_URL, tests=("campaigns-list-smoke",)) -> dict:
    """A project + one target + N tests bound to it (verified_against = the target KEY, which is what
    the launch derivation resolves). Unique `key` per test keeps the api_contract lane parallel-safe."""
    target_id = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                         target_key=key, base_url=base_url, timezone_="UTC",
                                         profile={"probe_path": "/campaigns"})
    ids = {}
    for name in tests:
        b = _bundle(name)
        b["test_id"] = f"{name}-{key}"                      # distinct test_key per seed
        ids[name], _ = repo.upsert_test(engine, project_id="golden", test_key=b["test_id"],
                                        bundle=b, verified_against=key)
    return {"target_id": target_id, "target_key": key, "tests": ids}


def _poll_terminal(tc, sr_id: str, timeout_s: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout_s
    d = None
    while time.monotonic() < deadline:
        d = tc.get(f"/suite_runs/{sr_id}").json()
        if d["state"] in ("done", "parked"):
            return d
        time.sleep(0.2)
    raise AssertionError(f"suite_run {sr_id} not terminal after {timeout_s}s: {d}")


# ── catalog: labels are visible, filterable, and countable ────────────────────────────────────────
@pytest.mark.api_contract
def test_catalog_exposes_labels_filters_and_counts():
    engine = get_engine()
    lbl = f"smk{uuid.uuid4().hex[:6]}"                      # unique so counts are exact in a shared DB
    solo = f"one{uuid.uuid4().hex[:6]}"                     # carried by exactly one test
    ids = _seed(engine, f"lbl-cat-{uuid.uuid4().hex[:6]}",
                tests=("campaigns-list-smoke", "login-smoke"))
    a, b = ids["tests"]["campaigns-list-smoke"], ids["tests"]["login-smoke"]

    with TestClient(create_app(engine=engine)) as tc:
        assert tc.patch(f"/tests/{a}", json={"labels": [lbl, solo]}).status_code == 200
        assert tc.patch(f"/tests/{b}", json={"labels": [lbl]}).status_code == 200

        # the catalog read carries labels AND the canonical target key (what a client shows on the
        # run button) — without verified_against the console could not name the derived target.
        row = next(t for t in tc.get("/projects/golden/tests").json() if t["id"] == a)
        assert row["labels"] == [lbl, solo]
        assert row["verified_against"] == ids["target_key"]

        filtered = tc.get("/projects/golden/tests", params={"label": lbl}).json()
        assert {t["id"] for t in filtered} == {a, b}
        assert {t["id"] for t in tc.get("/projects/golden/tests", params={"label": solo}).json()} == {a}

        # label= (empty) is the Unlabelled bucket — a distinct query from omitting the param
        unlabelled = tc.get("/projects/golden/tests", params={"label": ""}).json()
        assert a not in {t["id"] for t in unlabelled} and b not in {t["id"] for t in unlabelled}
        assert all(t["labels"] == [] for t in unlabelled)

        counts = {r["label"]: r["test_count"] for r in tc.get("/projects/golden/labels").json()}
        assert counts[lbl] == 2 and counts[solo] == 1


@pytest.mark.api_contract
def test_patch_normalizes_rejects_offshape_and_never_mints_a_version():
    engine = get_engine()
    ids = _seed(engine, f"lbl-patch-{uuid.uuid4().hex[:6]}")
    tid = ids["tests"]["campaigns-list-smoke"]

    with TestClient(create_app(engine=engine)) as tc:
        before = tc.get(f"/tests/{tid}/versions").json()

        # trimmed, lowercased, de-duplicated, order preserved
        r = tc.patch(f"/tests/{tid}", json={"labels": ["  SMOKE ", "smoke", "p0"]})
        assert r.status_code == 200, r.text
        assert r.json()["labels"] == ["smoke", "p0"]

        for bad in ("Smoke Test!", "-leading", "a" * 33, ""):
            assert tc.patch(f"/tests/{tid}", json={"labels": [bad]}).status_code == 422, bad
        assert tc.patch(f"/tests/{tid}", json={"labels": [f"l{i}" for i in range(17)]}).status_code == 422

        assert tc.patch(f"/tests/{uuid.uuid4()}", json={"labels": ["smoke"]}).status_code == 404

        # a rejected patch left the stored set untouched
        assert tc.get(f"/tests/{tid}").json()["labels"] == ["smoke", "p0"]

        # relabelling is auditable but is NOT a behaviour change: changelog yes, lineage no
        assert tc.get(f"/tests/{tid}/versions").json() == before
        kinds = [c["kind"] for c in tc.get("/projects/golden/changelog").json()
                 if c.get("test_id") == tid]
        assert "labels" in kinds


# ── running a label: the target is derived, never guessed ─────────────────────────────────────────
@pytest.mark.api_contract
def test_label_run_derives_target_and_freezes_membership():
    engine = get_engine()
    lbl = f"run{uuid.uuid4().hex[:6]}"
    ids = _seed(engine, f"lbl-run-{uuid.uuid4().hex[:6]}",
                tests=("campaigns-list-smoke", "login-smoke"))
    a, b = ids["tests"]["campaigns-list-smoke"], ids["tests"]["login-smoke"]

    with TestClient(create_app(engine=engine)) as tc:
        tc.patch(f"/tests/{a}", json={"labels": [lbl]})
        tc.patch(f"/tests/{b}", json={"labels": [lbl]})

        # NO target_id: the API resolves it from the members' verified_against
        r = tc.post(f"/projects/golden/labels/{lbl}/runs", params={"trigger": "manual"})
        assert r.status_code == 202, r.text
        sr_id = r.json()["suite_run_id"]

        with engine.connect() as c:                         # known at 202 (created synchronously)
            assert c.execute(text("select target_id from suite_run where id=:s"),
                             {"s": sr_id}).one().target_id == uuid.UUID(ids["target_id"])
            assert c.execute(text("select s.name from suite_run sr join suite s on s.id=sr.suite_id "
                                  "where sr.id=:s"), {"s": sr_id}).one().name == f"label:{lbl}"

        sr = _poll_terminal(tc, sr_id)
        assert sr["verdict"] in ("green", "green_with_heals"), sr
        assert len(sr["results"]) == 2, sr
        with engine.connect() as c:
            # the suite is the QUERY; the run_task rows are that run's frozen ANSWER (written by the
            # run thread at expansion, so this is asserted once the run is terminal)
            assert set(c.execute(text("select test_id from run_task where suite_run_id=:s"),
                                 {"s": sr_id}).scalars()) == {uuid.UUID(a), uuid.UUID(b)}


@pytest.mark.api_contract
def test_quarantined_member_is_excluded_from_the_launched_set():
    engine = get_engine()
    lbl = f"qtn{uuid.uuid4().hex[:6]}"
    ids = _seed(engine, f"lbl-qtn-{uuid.uuid4().hex[:6]}", base_url="http://localhost:1",
                tests=("campaigns-list-smoke", "login-smoke"))
    a, b = ids["tests"]["campaigns-list-smoke"], ids["tests"]["login-smoke"]

    with TestClient(create_app(engine=engine)) as tc:
        tc.patch(f"/tests/{a}", json={"labels": [lbl]})
        tc.patch(f"/tests/{b}", json={"labels": [lbl]})
        with engine.begin() as c:
            c.execute(text("update test set quarantined=true where id=:t"), {"t": b})

        r = tc.post(f"/projects/golden/labels/{lbl}/runs")
        assert r.status_code == 202, r.text
        sr_id = r.json()["suite_run_id"]
        _poll_terminal(tc, sr_id)                          # unreachable target → parks; lock released
        with engine.connect() as c:
            # the console still LISTS the quarantined member (marked); the RUN must not include it
            assert set(c.execute(text("select test_id from run_task where suite_run_id=:s"),
                                 {"s": sr_id}).scalars()) == {uuid.UUID(a)}
            assert c.execute(text("select member_test_ids from suite where name=:n"),
                             {"n": f"label:{lbl}"}).one().member_test_ids == [uuid.UUID(a)]


@pytest.mark.api_contract
def test_label_spanning_two_canonical_targets_is_refused_with_the_split():
    engine = get_engine()
    lbl = f"spn{uuid.uuid4().hex[:6]}"
    sfx = uuid.uuid4().hex[:6]
    one = _seed(engine, f"lbl-spanA-{sfx}", base_url="http://localhost:1")
    two = _seed(engine, f"lbl-spanB-{sfx}", base_url="http://localhost:1",
                tests=("login-smoke",))
    a, b = one["tests"]["campaigns-list-smoke"], two["tests"]["login-smoke"]

    with TestClient(create_app(engine=engine)) as tc:
        tc.patch(f"/tests/{a}", json={"labels": [lbl]})
        tc.patch(f"/tests/{b}", json={"labels": [lbl]})

        r = tc.post(f"/projects/golden/labels/{lbl}/runs")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "label spans targets"
        assert {(s["target_key"], s["test_count"]) for s in detail["split"]} == \
               {(one["target_key"], 1), (two["target_key"], 1)}

        # refusing must not have created a run — nothing to decode, nothing to clean up
        with engine.connect() as c:
            assert c.execute(text("select count(*) from suite_run sr join suite s on s.id=sr.suite_id "
                                  "where s.name=:n"), {"n": f"label:{lbl}"}).scalar() == 0

        # an explicit target_id is a deliberate cross-environment run and still works
        r2 = tc.post(f"/projects/golden/labels/{lbl}/runs",
                     params={"target_id": one["target_id"]})
        assert r2.status_code == 202, r2.text
        _poll_terminal(tc, r2.json()["suite_run_id"])


@pytest.mark.api_contract
def test_all_members_quarantined_says_so_instead_of_claiming_the_label_is_unused():
    """"no test carries this label" and "every test carrying it is sidelined" are different operator
    problems; reporting the second as the first sends someone hunting a typo that isn't there."""
    engine = get_engine()
    lbl = f"allq{uuid.uuid4().hex[:6]}"
    ids = _seed(engine, f"lbl-allq-{uuid.uuid4().hex[:6]}", base_url="http://localhost:1")
    tid = ids["tests"]["campaigns-list-smoke"]

    with TestClient(create_app(engine=engine)) as tc:
        tc.patch(f"/tests/{tid}", json={"labels": [lbl]})
        with engine.begin() as c:
            c.execute(text("update test set quarantined=true where id=:t"), {"t": tid})

        r = tc.post(f"/projects/golden/labels/{lbl}/runs")
        assert r.status_code == 404, r.text
        detail = r.json()["detail"]
        assert "quarantined" in detail and "1 test(s)" in detail, detail
        assert "no tests carry" not in detail, detail
        with engine.connect() as c:                        # still no run row created
            assert c.execute(text("select count(*) from suite where name=:n"),
                             {"n": f"label:{lbl}"}).scalar() == 0


@pytest.mark.api_contract
def test_unused_label_404s_before_any_run_row_exists():
    engine = get_engine()
    _seed(engine, f"lbl-404-{uuid.uuid4().hex[:6]}")
    with TestClient(create_app(engine=engine)) as tc:
        r = tc.post("/projects/golden/labels/nosuchlabel/runs")
        assert r.status_code == 404, r.text
        with engine.connect() as c:
            assert c.execute(text("select count(*) from suite where name=:n"),
                             {"n": "label:nosuchlabel"}).scalar() == 0


@pytest.mark.api_contract
def test_busy_target_409s_a_label_run():
    engine = get_engine()
    lbl = f"bsy{uuid.uuid4().hex[:6]}"
    ids = _seed(engine, f"lbl-busy-{uuid.uuid4().hex[:6]}", base_url="http://localhost:1")
    with TestClient(create_app(engine=engine)) as tc:
        tc.patch(f"/tests/{ids['tests']['campaigns-list-smoke']}", json={"labels": [lbl]})
        # hold the launch lock AFTER startup (the reaper parks pre-existing actives)
        blocker = repo.create_suite(engine, project_id="golden", name=f"blocker-{lbl}",
                                    member_test_ids=[])
        active = repo.create_suite_run(engine, project_id="golden", suite_id=blocker,
                                       target_id=ids["target_id"])
        r = tc.post(f"/projects/golden/labels/{lbl}/runs")
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {"error": "target busy", "active_suite_run_id": active}
        repo.set_suite_run_state(engine, suite_run_id=active, state="done", verdict="green")


@pytest.mark.api_contract
def test_authoring_rejects_an_offshape_label_before_doing_any_work():
    """Label validation precedes the streamed author (browser + model): a typo is a fast 422, never an
    in-band error after a minute of work."""
    engine = get_engine()
    ids = _seed(engine, f"lbl-auth-{uuid.uuid4().hex[:6]}")
    with TestClient(create_app(engine=engine)) as tc:
        r = tc.post("/projects/golden/tests",
                    params={"test_key": "never-authored", "target_id": ids["target_id"],
                            "labels": ["Smoke Test!"]},
                    json={"nl": "irrelevant — must 422 before authoring starts"})
        assert r.status_code == 422, r.text
        with engine.connect() as c:
            assert c.execute(text("select count(*) from test where test_key='never-authored'"
                                  " and project_id='golden'")).scalar() == 0
