"""The `holdout` marker (GOAL rule 9) — generalization on flows the build agent NEVER saw.

`verify.sh holdout` runs this against `$ANNEAL_HOLDOUT_DIR` — a directory OUTSIDE this repo, populated
by the human with real flows from the parents. The runner authors each unseen NL flow through the HOT
lane against its named target and asserts the frozen bundle REPLAYS to its expected verdict. Passing
means the engine generalizes; failing means it overfit to the corpus (a failure even with a green gate).

The build implements the runner; it never populates the dir.

── holdout.json schema (the human writes this to match the runner) ──────────────────────────────────
{
  "targets": {                       // optional per-target profile; 'golden' defaults to the live app
    "golden": { "base_url": "http://localhost:8099", "probe_path": "/campaigns", "persona": "member" }
  },
  "flows": [
    { "nl": "<name>.nl.txt",         // file in $ANNEAL_HOLDOUT_DIR holding the NL flow text
      "target": "golden",            // target name (only 'golden' is wired now; 'cart'/'live' at M4/parity)
      "expect": "green",             // expected replay verdict (D29): green | green_with_heals | red | ...
      "answers": { "<q>": "<a>" } }  // optional: pre-answers for the D26 ambiguity interview
  ]
}
A flat map { "<name>.nl.txt": {"target": "...", "expect": "..."} } is also accepted.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest
from sqlalchemy import text

from anneal_artifacts.store import LocalDiskStore
from anneal_authoring.calibrate import CalibrationError
from anneal_authoring.service import author_and_freeze
from anneal_db import repositories as repo
from anneal_db.engine import get_engine

from conftest import GOLDEN_URL

_DIR = os.getenv("ANNEAL_HOLDOUT_DIR")
_TABLES = ["step_event", "incident_member", "run_result", "run_task", "test_version", "test_golden", "suite_run",
           "suite", "changelog", "incident", "test", "module_version", "module", "session",
           "credential", "territory_notes", "target", "project"]
_PASS = {"green", "green_with_heals"}


def _load_flows() -> list[dict]:
    """Parse holdout.json into a normalized flow list; return [] when no dir/manifest is present (e.g.
    collection under other markers), so the parametrization is empty and nothing runs vacuously."""
    if not _DIR or not os.path.isdir(_DIR):
        return []
    manifest_path = pathlib.Path(_DIR) / "holdout.json"
    if not manifest_path.is_file():
        return []                                               # verify.sh already fails a dir with no manifest
    manifest = json.loads(manifest_path.read_text())
    targets = manifest.get("targets", {}) if isinstance(manifest, dict) else {}
    raw = manifest.get("flows") if isinstance(manifest, dict) and "flows" in manifest else manifest
    flows = []
    if isinstance(raw, list):
        for f in raw:
            if isinstance(f, dict) and f.get("nl"):             # skip malformed entries, never crash collection
                flows.append({**f, "_targets": targets})
    elif isinstance(raw, dict):                                  # flat { "<name>.nl.txt": {...} }
        for nl, spec in raw.items():
            if isinstance(spec, dict):
                flows.append({"nl": nl, **spec, "_targets": targets})
    return flows


def _target_profile(flow: dict) -> tuple[str, dict]:
    name = flow.get("target", "golden")
    prof = (flow.get("_targets") or {}).get(name, {})
    if name != "golden" and not prof.get("base_url"):
        pytest.fail(f"holdout target {name!r} needs a base_url in holdout.json")
    return name, {"base_url": prof.get("base_url") or GOLDEN_URL,
                  # default to the AUTHENTICATED entry, not '/' (which redirects to sign-in → parked_auth)
                  "probe_path": prof.get("probe_path") or ("/campaigns" if name == "golden" else "/"),
                  "persona": prof.get("persona", "member"),
                  # a real target logs in via its auth block {"kind": form|clerk|auto, "signin_path",
                  # "creds_ref"}; creds resolve through the vault chain (ANNEAL_<CREDS_REF>_USERNAME/_PASSWORD)
                  "auth": prof.get("auth")}


def _seed_session(eng, *, project_id, persona, target_id, storage_path):
    """A replay-valid seeded session (D14) so the §9 probe validates a SEEDED session and the flow
    actually REPLAYS instead of parking AUTH_EXPIRED — the same shape the m2/m1 tests use. The
    storage-state file is a real (empty-but-valid) Playwright state so `recording` can inject it.
    Sessions are keyed (credential_id, target_id) since migration 0003, so the seed names the target."""
    pathlib.Path(storage_path).write_text(json.dumps({"cookies": [], "origins": []}))
    with eng.begin() as c:
        cred = c.execute(text("insert into credential (project_id, persona, secret_enc) "
                              "values (:p, :persona, '\\x00') "
                              "on conflict (project_id, persona) do update set persona = excluded.persona "
                              "returning id"), {"p": project_id, "persona": persona}).scalar()
        c.execute(text("insert into session (credential_id, target_id, storage_state_path, state) "
                       "values (:c, :t, :sp, 'valid') on conflict (credential_id, target_id) "
                       "do update set storage_state_path = excluded.storage_state_path, state = 'valid'"),
                  {"c": cred, "t": target_id, "sp": storage_path})


def _reset_golden(base_url: str):
    """The golden mock exposes /__reset (test-only) to restore its seed state between flows."""
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(
            base_url.rstrip("/") + "/__reset", data=b"{}",
            headers={"content-type": "application/json"}, method="POST"), timeout=10).read()
    except Exception:
        pass                                                    # a target without /__reset just proceeds


@pytest.fixture(scope="module")
def _clean_db():
    eng = get_engine()
    with eng.begin() as c:
        for t in _TABLES:
            c.execute(text(f"delete from {t}"))
    return eng


@pytest.mark.holdout
@pytest.mark.parametrize("flow", _load_flows(), ids=lambda f: f.get("nl", "?"))
def test_holdout_flow_generalizes(flow, _clean_db, tmp_path):
    """Author one unseen flow through the hot lane and assert its frozen bundle replays to expected."""
    from anneal_scheduler.scheduler import run_suite

    eng = _clean_db
    name, prof = _target_profile(flow)
    expect = flow.get("expect", "green")
    nl = (pathlib.Path(_DIR) / flow["nl"]).read_text().strip()
    key = flow["nl"].replace(".nl.txt", "").replace(".", "-")

    if name == "golden":                                        # the hermetic mock resets to a clean
        _reset_golden(prof["base_url"])                         # state so each flow runs in isolation

    profile = {"probe_path": prof["probe_path"]}
    if prof["auth"]:
        profile["auth"] = prof["auth"]
    tid = repo.seed_project_target(eng, project_id="holdout", project_name="Holdout",
                                   target_key=f"holdout-{key}", base_url=prof["base_url"],
                                   timezone_="UTC", profile=profile)
    if not prof["auth"]:                                        # golden has no login wall: an empty state
        _seed_session(eng, project_id="holdout", persona=prof["persona"], target_id=tid,   # is "valid"
                      storage_path=str(tmp_path / "session.json"))
    # else: author_and_freeze's _ensure_session logs in through the auth block and seeds the session
    # the scheduler probe validates — the same path a real onboarded target takes.

    try:
        res = _run(author_and_freeze(eng, project_id="holdout", test_key=key, nl=nl, target_id=tid,
                                     persona=prof["persona"], answers=flow.get("answers")))
    except CalibrationError as e:
        # D7: an oracle that is never true on a clean replay is REFUSED at freeze, not frozen as a red
        # test — for a red-by-design negative that refusal is the honest verdict (it never went green).
        assert expect == "red", f"{flow['nl']}: calibration refused the freeze: {e}"
        return
    if res.get("status") == "needs_answers":
        assert expect in ("needs_answers", "blocked"), \
            f"{flow['nl']}: hot lane could not author it (interview blocked): {res.get('questions')}"
        return
    assert res.get("status") == "frozen", f"{flow['nl']}: authoring did not freeze: {res}"

    suite = repo.create_suite(eng, project_id="holdout", name=f"holdout-{key}",
                              member_test_ids=[res["test_id"]])
    art = LocalDiskStore(str(tmp_path / "holdoutart"))
    sr = repo.create_suite_run(eng, project_id="holdout", suite_id=suite, target_id=tid)
    report = _run(run_suite(eng, art, suite_run_id=sr, target_id=tid, max_parallel=1))

    ok = report.verdict == expect or (expect == "green" and report.verdict in _PASS)
    assert ok, f"{flow['nl']}: replayed {report.verdict!r}, expected {expect!r} — GENERALIZATION FAILURE"


def _run(coro):
    """author_and_freeze / run_suite are async; run them to completion."""
    import asyncio
    import inspect
    if inspect.isawaitable(coro):
        return asyncio.run(coro)
    return coro
