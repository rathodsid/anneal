"""Multi-platform auth — hermetic gate (T1–T25, AUTH-PLAN.md test matrix).

These tests are
the auth bar (verify-auth.sh greps for the 25 exact names, forbids skip/xfail, requires them green
against the hermetic golden app + postgres).

Layout:
  Phase B  T1–T7   credential resolution chain + Fernet vault (no browser)
  Phase C  T8–T15  anneal_executor/login.py strategies against the Phase A fixtures
  Phase D  T16–T23 session lifecycle + probe gate (integration: scheduler.run_suite)
  Phase E  T24–T25 onboarding endpoint + CLI
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from anneal_db import vault
from anneal_db.engine import get_engine
from anneal_db.vault import Credentials
from anneal_executor.login import LoginOutcome, login

from conftest import GOLDEN_URL

_TEST_VAULT_KEY = Fernet.generate_key().decode()   # one valid Fernet key per test run

# FK-safe wipe order (children before parents). The postgres volume PERSISTS across gate runs and the
# golden regression (m1–m4 / parity / holdout) leaves incident / incident_member / changelog rows that
# FK-reference project/test/run_result — so those MUST be wiped before their parents or an unqualified
# `delete from project` FK-violates on every run after the first. Mirrors test_m1.py's proven order.
_AUTH_TABLES = ["step_event", "incident_member", "run_result", "run_task", "test_version", "test_golden",
                "suite_run", "suite", "changelog", "incident", "test", "module_version", "module",
                "session", "credential", "territory_notes", "target", "project"]


@pytest.fixture
def engine():
    return get_engine()


@pytest.fixture(autouse=True)
def _isolated_db(engine):
    """Wipe the auth-relevant tables (FK-safe) before every test so credential/session rows from one
    test never leak into another's resolution chain."""
    with engine.begin() as c:
        for t in _AUTH_TABLES:
            c.execute(text(f"delete from {t}"))
    yield


@pytest.fixture
def vault_key(monkeypatch):
    """A present, valid ANNEAL_VAULT_KEY for the tests that store/read encrypted secrets."""
    monkeypatch.setenv("ANNEAL_VAULT_KEY", _TEST_VAULT_KEY)
    return _TEST_VAULT_KEY


@pytest.fixture
def _clean_cred_env(monkeypatch):
    """Ensure no ambient ANNEAL_*_USERNAME/_PASSWORD env layers bleed into resolution assertions."""
    import os
    for k in list(os.environ):
        if k.startswith("ANNEAL_") and (k.endswith("_USERNAME") or k.endswith("_PASSWORD")):
            monkeypatch.delenv(k, raising=False)


def _seed_project(engine, project_id="proj-a"):
    with engine.begin() as c:
        c.execute(text("insert into project (id,name) values (:i,:n) on conflict (id) do nothing"),
                  {"i": project_id, "n": project_id})
    return project_id


# ── Phase B — T1–T7: resolution chain + Fernet envelope ──────────────────────────────────────────
def test_resolve_db_layer_wins(engine, vault_key, monkeypatch, _clean_cred_env):
    """T1: a DB credential (encrypted) is returned even when both env layers are ALSO set."""
    pid = _seed_project(engine)
    vault.store_credential(engine, project_id=pid, persona="member",
                           username="db-user", password="db-pass")
    monkeypatch.setenv("ANNEAL_PROJ_A_USERNAME", "env-ref-user")
    monkeypatch.setenv("ANNEAL_PROJ_A_PASSWORD", "env-ref-pass")
    monkeypatch.setenv("ANNEAL_UI_USERNAME", "env-ui-user")
    monkeypatch.setenv("ANNEAL_UI_PASSWORD", "env-ui-pass")
    creds = vault.resolve(engine, creds_ref="proj-a", project_id=pid, persona="member")
    assert creds == vault.Credentials("db-user", "db-pass")


def test_resolve_per_ref_env(engine, vault_key, monkeypatch, _clean_cred_env):
    """T2: no DB row → ANNEAL_<REF>_* used, with ref normalization (acme-staging → ACME_STAGING)."""
    pid = _seed_project(engine)
    monkeypatch.setenv("ANNEAL_ACME_STAGING_USERNAME", "acme-user")
    monkeypatch.setenv("ANNEAL_ACME_STAGING_PASSWORD", "acme-pass")
    monkeypatch.setenv("ANNEAL_UI_USERNAME", "generic-user")   # per-ref must win over generic
    monkeypatch.setenv("ANNEAL_UI_PASSWORD", "generic-pass")
    creds = vault.resolve(engine, creds_ref="acme-staging", project_id=pid, persona="member")
    assert creds == vault.Credentials("acme-user", "acme-pass")


def test_resolve_generic_env_fallback(engine, vault_key, monkeypatch, _clean_cred_env):
    """T3: only ANNEAL_UI_* set (no DB row, no per-ref env) → the generic fallback is used."""
    pid = _seed_project(engine)
    monkeypatch.setenv("ANNEAL_UI_USERNAME", "ui-user")
    monkeypatch.setenv("ANNEAL_UI_PASSWORD", "ui-pass")
    creds = vault.resolve(engine, creds_ref="whatever-ref", project_id=pid, persona="member")
    assert creds == vault.Credentials("ui-user", "ui-pass")


def test_resolve_nothing_anywhere(engine, vault_key, _clean_cred_env):
    """T4: nothing in any layer → a structured error that NAMES the ref (becomes the park reason),
    not a raw exception trace."""
    pid = _seed_project(engine)
    with pytest.raises(vault.CredentialsNotFound) as ei:
        vault.resolve(engine, creds_ref="ghost-ref", project_id=pid, persona="member")
    assert "ghost-ref" in str(ei.value)


def test_vault_roundtrip(vault_key):
    """T5: encrypt → (bytes) → decrypt round-trips the credentials JSON exactly."""
    enc = vault.encrypt_credentials("round-user", "round-pass")
    assert isinstance(enc, bytes)
    assert vault.decrypt_credentials(enc) == vault.Credentials("round-user", "round-pass")


def test_vault_key_missing_refuses(engine, monkeypatch, _clean_cred_env):
    """T6: with ANNEAL_VAULT_KEY absent, BOTH store and read refuse, the error names the var, and a
    keyless store writes NOTHING. The read side is the real downgrade guard — a present DB row with the
    key removed must refuse, NOT fall through to env (§3.3)."""
    pid = _seed_project(engine)
    # store refuse (no key) — and nothing written
    monkeypatch.delenv("ANNEAL_VAULT_KEY", raising=False)
    with pytest.raises(vault.VaultKeyMissing) as se:
        vault.store_credential(engine, project_id=pid, persona="member", username="u", password="p")
    assert "ANNEAL_VAULT_KEY" in str(se.value)
    with engine.connect() as c:
        n = c.execute(text("select count(*) from credential where project_id=:p"), {"p": pid}).scalar()
    assert n == 0, "a keyless store must write NOTHING"
    # read refuse: seed a real encrypted row WITH a key, then remove the key and resolve → must refuse
    monkeypatch.setenv("ANNEAL_VAULT_KEY", _TEST_VAULT_KEY)
    vault.store_credential(engine, project_id=pid, persona="member", username="u", password="p")
    monkeypatch.delenv("ANNEAL_VAULT_KEY", raising=False)
    monkeypatch.setenv("ANNEAL_UI_USERNAME", "env-u")   # present so a WRONG fall-through would "succeed"
    monkeypatch.setenv("ANNEAL_UI_PASSWORD", "env-p")
    with pytest.raises(vault.VaultKeyMissing) as re_:
        vault.resolve(engine, creds_ref="proj-a", project_id=pid, persona="member")
    assert "ANNEAL_VAULT_KEY" in str(re_.value), "read must refuse (named), never fall through to env"


def test_no_plaintext_in_db(engine, vault_key):
    """T7: after storing, the raw `secret_enc` bytes do NOT contain the plaintext password substring."""
    pid = _seed_project(engine)
    secret = "sup3r-s3cret-fixture-pass"
    vault.store_credential(engine, project_id=pid, persona="member",
                           username="member-user", password=secret)
    with engine.connect() as c:
        raw = c.execute(text("select secret_enc from credential where project_id=:p and persona='member'"),
                        {"p": pid}).scalar_one()
    raw = bytes(raw)
    assert secret.encode() not in raw, "plaintext password leaked into secret_enc"
    assert b"member-user" not in raw, "plaintext username leaked into secret_enc"
    # and it really is our envelope: decrypting recovers the JSON shape
    assert json.loads(Fernet(_TEST_VAULT_KEY).decrypt(raw)) == {"username": "member-user", "password": secret}


# ── Phase C — T8–T15: login.py strategies against the Phase A fixtures ────────────────────────────
from playwright.async_api import async_playwright  # noqa: E402

_FIXTURE_CREDS = Credentials("fixture-user", "fixture-pass")


class _LoginResult:
    def __init__(self, outcome, stats, private_reachable, storage_state, final_url):
        self.outcome = outcome
        self.stats = stats
        self.private_reachable = private_reachable
        self.storage_state = storage_state
        self.final_url = final_url


async def _do_login(auth_cfg, creds, *, save_path=None, check_private=True):
    """Drive login() once in a fresh hermetic browser context against the golden app, resetting the
    auth counters first. Returns the outcome, the /__auth_stats snapshot, whether /private is reachable
    with the resulting session, the saved storage_state, and the page's final URL."""
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            ctx = await b.new_context()
            page = await ctx.new_page()
            await page.request.post(f"{GOLDEN_URL}/__reset")
            outcome = await login(page, auth_cfg, creds, base_url=GOLDEN_URL)
            final_url = page.url
            private = False
            if check_private:
                await page.goto(f"{GOLDEN_URL}/private", wait_until="domcontentloaded")
                private = await page.locator("[data-testid=private-content]").count() > 0
            state = await ctx.storage_state(path=save_path)
            stats = await (await page.request.get(f"{GOLDEN_URL}/__auth_stats")).json()
        finally:
            await b.close()
    return _LoginResult(outcome, stats, private, state, final_url)


async def test_form_login_single_step(tmp_path):
    """T8: /login-form → LoginOutcome.ok; /private reachable; a usable storage_state is written."""
    ss = tmp_path / "form.json"
    r = await _do_login({"kind": "form", "signin_path": "/login-form"}, _FIXTURE_CREDS, save_path=str(ss))
    assert r.outcome.is_ok, r.outcome
    assert r.private_reachable, "private area not reachable after login"
    assert ss.exists() and any(c["name"] == "fixture_session" for c in r.storage_state["cookies"]), \
        "storage_state file not written with the session cookie"
    assert r.stats["login_attempts"] == 1 and r.stats["logins_ok"] == 1, r.stats


async def test_form_login_two_step():
    """T9: /login-2step email → Continue → password reveal → submit completes."""
    r = await _do_login({"kind": "form", "signin_path": "/login-2step"}, _FIXTURE_CREDS)
    assert r.outcome.is_ok, r.outcome
    assert r.private_reachable
    assert r.stats["login_attempts"] == 1


async def test_clerk_exact_continue_dodges_sso():
    """T10: /login-clerkish logs in via the EXACT 'Continue'; the decoy 'Continue with Google' is
    NEVER clicked (sso_trap_hits == 0)."""
    r = await _do_login({"kind": "clerk", "signin_path": "/login-clerkish"}, _FIXTURE_CREDS)
    assert r.outcome.is_ok, r.outcome
    assert r.private_reachable
    assert r.stats["sso_trap_hits"] == 0, "the SSO decoy was clicked — login hijacked into /sso-trap"


async def test_fill_verified_retries():
    """T11: /login-fillclear drops the first programmatic fill; login still succeeds → the
    click+type retry path in _fill_verified landed the value (an un-retried empty username → 401)."""
    r = await _do_login({"kind": "form", "signin_path": "/login-fillclear"}, _FIXTURE_CREDS)
    assert r.outcome.is_ok, r.outcome
    assert r.private_reachable, "retry path did not land the username → login failed"
    assert r.stats["logins_ok"] == 1


async def test_hard_auth_captcha():
    """T12: /login-captcha → hard_auth, reason mentions captcha, and ZERO credential submissions."""
    r = await _do_login({"kind": "form", "signin_path": "/login-captcha"}, _FIXTURE_CREDS,
                        check_private=False)
    assert r.outcome.needs_human and r.outcome.status == "hard_auth", r.outcome
    assert "captcha" in (r.outcome.reason or "").lower(), r.outcome.reason
    assert r.stats["login_attempts"] == 0, "credentials were submitted despite a captcha (T12)"


async def test_hard_auth_otp():
    """T13: /login-otp → hard_auth, reason mentions OTP/2FA; zero submissions."""
    r = await _do_login({"kind": "form", "signin_path": "/login-otp"}, _FIXTURE_CREDS,
                        check_private=False)
    assert r.outcome.needs_human and r.outcome.status == "hard_auth", r.outcome
    reason = (r.outcome.reason or "").lower()
    assert "otp" in reason or "2fa" in reason, r.outcome.reason
    assert r.stats["login_attempts"] == 0


async def test_bad_password_fails_once():
    """T14: wrong creds → failed(reason); exactly one attempt, no retry loop."""
    r = await _do_login({"kind": "form", "signin_path": "/login-form"},
                        Credentials("fixture-user", "wrong-pass"), check_private=False)
    assert r.outcome.status == "failed", r.outcome
    assert r.stats["login_attempts"] == 1, ("exactly one attempt, no retry loop", r.stats)


async def test_auth_none_noop():
    """T15: kind:none → ok, the strategy NEVER navigates (page stays blank) and never submits."""
    r = await _do_login({"kind": "none"}, _FIXTURE_CREDS, check_private=False)
    assert r.outcome.is_ok, r.outcome
    assert r.final_url in ("about:blank", ""), f"kind:none navigated to {r.final_url}"
    assert r.stats["login_attempts"] == 0


async def test_recaptcha_v3_not_hard_auth():
    """(beyond the T1–T25 matrix) PASSIVE reCAPTCHA v3 is NOT a hard-auth block: /login-recaptcha-v3
    carries the invisible v3 badge (a real-world Clerk shape), so the engine must IGNORE it and log in — NOT
    park needs_human. Guards against hard_auth_signal over-deferring on score-based bot detection."""
    r = await _do_login({"kind": "form", "signin_path": "/login-recaptcha-v3"}, _FIXTURE_CREDS)
    assert r.outcome.is_ok, ("passive reCAPTCHA v3 must not defer", r.outcome)
    assert not r.outcome.needs_human
    assert r.private_reachable
    assert r.stats["login_attempts"] == 1, "the engine should have submitted (not deferred on v3)"


# ── Phase D — T16–T23: session lifecycle + probe gate (integration via run_suite) ────────────────
import httpx  # noqa: E402

from anneal_artifacts.store import LocalDiskStore  # noqa: E402
from anneal_db import repositories as repo  # noqa: E402
from anneal_scheduler.scheduler import run_suite  # noqa: E402

_FORM_AUTH = {"kind": "form", "signin_path": "/login-form"}


def _reset_stats():
    httpx.post(f"{GOLDEN_URL}/__reset")


def _stats():
    return httpx.get(f"{GOLDEN_URL}/__auth_stats").json()


def _auth_bundle(project, target_key):
    """A minimal frozen bundle for an auth 'platform': open /private and confirm the protected content
    renders. The run's context is seeded from the persona's session storage_state, so this is green iff
    the probe gate produced a valid session (reuse or soft re-login)."""
    return {
        "schema_version": 1, "project_id": project, "test_id": "auth-private-smoke", "version": 1,
        "state": "frozen", "persona": "member", "params": {},
        "lineage": {"parent": None, "reason": "authored", "ruling": "canonical", "incident": None,
                    "triggered_by": "auth_seed", "verified_against": target_key},
        "intent": {"sources": [{"kind": "nl",
                                 "verbatim": "Confirm the private area renders for an authenticated session."}],
                   "descriptor": "private area renders when authenticated"},
        "policy": {"exclusive": False, "mutating": False},
        "steps": [
            {"id": "s1", "intent": "open the private area", "action": "goto", "arg": "/private",
             "postcondition": {"all": [{"kind": "element", "state": "visible",
                                        "target": {"strategy": "testid", "selector": "private-content"}}],
                               "budget_ms": 10000}},
            {"id": "s2", "intent": "confirm the private content is visible", "action": "assert",
             "assert": {"state": "visible",
                        "target": {"strategy": "testid", "selector": "private-content"}},
             "postcondition": None, "budget_ms": 8000},
        ],
    }


def _seed_auth_platform(engine, *, project, target_key, auth, probe_path="/private"):
    """Seed a project + auth-configured target + the auth-private-smoke test + a suite binding it."""
    _seed_project(engine, project)
    tid = repo.seed_project_target(engine, project_id=project, project_name=project,
                                   target_key=target_key, base_url=GOLDEN_URL, timezone_="UTC",
                                   profile={"probe_path": probe_path, "auth": auth})
    test_id, _ = repo.upsert_test(engine, project_id=project, test_key="auth-private-smoke",
                                  bundle=_auth_bundle(project, target_key), verified_against=target_key)
    suite = repo.create_suite(engine, project_id=project, name=f"auth-{target_key}",
                              member_test_ids=[test_id])
    return tid, suite, test_id


async def _make_storage_state(auth_cfg, creds, path):
    """Log in for real (once, in setup) and save a reusable storage_state to `path`."""
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            ctx = await b.new_context()
            outcome = await login(await ctx.new_page(), auth_cfg, creds, base_url=GOLDEN_URL)
            assert outcome.is_ok, outcome
            await ctx.storage_state(path=path)
        finally:
            await b.close()


def _blank_storage_state(path):
    Path(path).write_text(json.dumps({"cookies": [], "origins": []}))


async def _run(engine, tmp_path, project, suite, tid, sub="art"):
    sr = repo.create_suite_run(engine, project_id=project, suite_id=suite, target_id=tid)
    return await run_suite(engine, LocalDiskStore(str(tmp_path / sub)), suite_run_id=sr,
                           target_id=tid, max_parallel=1)


async def test_probe_valid_skips_login(engine, vault_key, tmp_path, _clean_cred_env):
    """T16 (AC3): a valid seeded session → suite green, login_attempts == 0 (never re-logs-in)."""
    project = "plat-valid"
    tid, suite, _ = _seed_auth_platform(engine, project=project, target_key="valid-form", auth=_FORM_AUTH)
    ss = str(tmp_path / "valid.json")
    await _make_storage_state(_FORM_AUTH, _FIXTURE_CREDS, ss)
    cid = repo.ensure_credential_anchor(engine, project_id=project, persona="member")
    repo.upsert_session(engine, credential_id=cid, target_id=tid, storage_state_path=ss, state="valid")
    _reset_stats()
    report = await _run(engine, tmp_path, project, suite, tid)
    assert report.verdict == "green", report
    assert _stats()["login_attempts"] == 0, "a valid session must never re-login (AC3)"


async def test_probe_expired_soft_relogin(engine, vault_key, tmp_path, _clean_cred_env):
    """T17 (AC2): a blank storage_state → probe expired → exactly ONE soft re-login → suite green,
    login_attempts == 1, and the session row is updated (state='valid', fresh seeded_at)."""
    project = "plat-expired"
    tid, suite, _ = _seed_auth_platform(engine, project=project, target_key="exp-form", auth=_FORM_AUTH)
    vault.store_credential(engine, project_id=project, persona="member",
                           username="fixture-user", password="fixture-pass")
    cid = repo.ensure_credential_anchor(engine, project_id=project, persona="member")
    ss = str(tmp_path / "blank.json"); _blank_storage_state(ss)
    repo.upsert_session(engine, credential_id=cid, target_id=tid, storage_state_path=ss, state="valid")
    with engine.connect() as c:
        before = c.execute(text("select seeded_at from session where credential_id=:c and target_id=:t"),
                           {"c": cid, "t": tid}).scalar()
    _reset_stats()
    report = await _run(engine, tmp_path, project, suite, tid)
    assert report.verdict == "green", report
    assert _stats()["login_attempts"] == 1, "expired → exactly one soft re-login"
    with engine.connect() as c:
        row = c.execute(text("select state, seeded_at, storage_state_path from session "
                             "where credential_id=:c and target_id=:t"), {"c": cid, "t": tid}).one()
    assert row.state == "valid"
    assert row.seeded_at > before, "session row not re-seeded (fresh seeded_at)"
    assert row.storage_state_path != ss, "storage_state not re-pointed to the fresh login state"


async def test_probe_failed_login_parks(engine, vault_key, tmp_path, _clean_cred_env):
    """T18 (AC4): a wrong stored password → suite parks AUTH_EXPIRED with a verbatim reason, exactly
    ONE attempt, no retry loop, and NOT misclassified as a product failure."""
    project = "plat-badpass"
    tid, suite, _ = _seed_auth_platform(engine, project=project, target_key="bad-form", auth=_FORM_AUTH)
    vault.store_credential(engine, project_id=project, persona="member",
                           username="fixture-user", password="wrong-pass")
    cid = repo.ensure_credential_anchor(engine, project_id=project, persona="member")
    ss = str(tmp_path / "blank.json"); _blank_storage_state(ss)
    repo.upsert_session(engine, credential_id=cid, target_id=tid, storage_state_path=ss, state="valid")
    _reset_stats()
    report = await _run(engine, tmp_path, project, suite, tid)
    assert report.state == "parked" and report.verdict == "parked_auth", report
    assert report.park_reason == "AUTH_EXPIRED"
    assert report.auth_reason and not report.needs_human, report
    assert _stats()["login_attempts"] == 1, "exactly one attempt, no retry loop"
    assert report.results == [], "no product run despite an auth failure"
    # the verbatim reason + needs_human are PERSISTED and EXPOSED on the API (auth UI, §6 gap #1)
    from fastapi.testclient import TestClient

    from anneal_api.app import create_app
    with TestClient(create_app(engine=engine, artifacts=LocalDiskStore(str(tmp_path / "api")))) as tc:
        detail = tc.get(f"/suite_runs/{report.suite_run_id}").json()
    assert detail["park_reason"] == "AUTH_EXPIRED", detail
    assert detail["auth_reason"] == report.auth_reason and detail["needs_human"] is False, detail


async def test_probe_hard_auth_parks_needs_human(engine, vault_key, tmp_path, _clean_cred_env):
    """T19 (AC4): a captcha target → park needs_human, reason mentions captcha, ZERO submissions."""
    project = "plat-captcha"
    tid, suite, _ = _seed_auth_platform(engine, project=project, target_key="cap-form",
                                        auth={"kind": "form", "signin_path": "/login-captcha"})
    vault.store_credential(engine, project_id=project, persona="member",
                           username="fixture-user", password="fixture-pass")
    cid = repo.ensure_credential_anchor(engine, project_id=project, persona="member")
    ss = str(tmp_path / "blank.json"); _blank_storage_state(ss)
    repo.upsert_session(engine, credential_id=cid, target_id=tid, storage_state_path=ss, state="valid")
    _reset_stats()
    report = await _run(engine, tmp_path, project, suite, tid)
    assert report.verdict == "parked_auth" and report.needs_human, report
    assert "captcha" in (report.auth_reason or "").lower(), report.auth_reason
    assert _stats()["login_attempts"] == 0, "zero submissions on a captcha (G5)"
    # needs_human + the verbatim reason are PERSISTED on the suite_run row (auth UI, §6 gap #1)
    row = repo.get_suite_run(engine, suite_run_id=report.suite_run_id)
    assert row["needs_human"] is True and "captcha" in (row["auth_reason"] or "").lower(), row


async def test_session_keyed_per_target(engine, vault_key, tmp_path, _clean_cred_env):
    """T20 (AC6): one persona × two targets = two independent session rows; re-logging into B does NOT
    clobber A's storage_state."""
    project = "plat-multi"
    tA, _, _ = _seed_auth_platform(engine, project=project, target_key="multi-a", auth=_FORM_AUTH)
    tB, sB, _ = _seed_auth_platform(engine, project=project, target_key="multi-b", auth=_FORM_AUTH)
    vault.store_credential(engine, project_id=project, persona="member",
                           username="fixture-user", password="fixture-pass")
    cid = repo.ensure_credential_anchor(engine, project_id=project, persona="member")
    # valid session on A (real login); blank on B (will soft-relogin on run)
    ssA = str(tmp_path / "a.json")
    await _make_storage_state(_FORM_AUTH, _FIXTURE_CREDS, ssA)
    repo.upsert_session(engine, credential_id=cid, target_id=tA, storage_state_path=ssA, state="valid")
    ssB = str(tmp_path / "b-blank.json"); _blank_storage_state(ssB)
    repo.upsert_session(engine, credential_id=cid, target_id=tB, storage_state_path=ssB, state="valid")
    with engine.connect() as c:
        n = c.execute(text("select count(*) from session where credential_id=:c"), {"c": cid}).scalar()
    assert n == 2, "one persona × two targets must be two independent rows"
    _reset_stats()
    report = await _run(engine, tmp_path, project, sB, tB)
    assert report.verdict == "green", report
    a_path = repo.get_storage_state(engine, project_id=project, persona="member", target_id=tA)
    b_path = repo.get_storage_state(engine, project_id=project, persona="member", target_id=tB)
    assert a_path == ssA, "logging into B clobbered A's session (per-target keying failed)"
    assert b_path and b_path != ssB, "B's session was not re-seeded by its own soft re-login"


async def test_no_creds_configured_parks(engine, vault_key, tmp_path, _clean_cred_env):
    """T21: a target with an auth block but NO credential in any layer → park, reason 'no credentials
    for <ref>', zero submissions."""
    project = "plat-nocreds"
    tid, suite, _ = _seed_auth_platform(engine, project=project, target_key="noc-form",
                                        auth={"kind": "form", "signin_path": "/login-form",
                                              "creds_ref": "noc-ref"})
    _reset_stats()
    report = await _run(engine, tmp_path, project, suite, tid)
    assert report.verdict == "parked_auth", report
    assert report.auth_reason and "no credentials for noc-ref" in report.auth_reason, report.auth_reason
    assert _stats()["login_attempts"] == 0


async def test_midsuite_expiry_unchanged(engine, vault_key, tmp_path, _clean_cred_env):
    """T22 (regression): v1 has NO mid-suite re-login — expiry is handled ONLY at the pre-suite probe
    gate. (a) a valid session runs the whole suite with ZERO logins and its session row is NOT re-seeded
    mid-run; (b) a gate park still cooperatively cancels queued tasks (nothing runs)."""
    # (a) no mid-suite re-login
    project = "plat-midsuite"
    tid, suite, _ = _seed_auth_platform(engine, project=project, target_key="mid-form", auth=_FORM_AUTH)
    ss = str(tmp_path / "valid.json")
    await _make_storage_state(_FORM_AUTH, _FIXTURE_CREDS, ss)
    cid = repo.ensure_credential_anchor(engine, project_id=project, persona="member")
    repo.upsert_session(engine, credential_id=cid, target_id=tid, storage_state_path=ss, state="valid")
    with engine.connect() as c:
        seeded_before = c.execute(text("select seeded_at from session where credential_id=:c and target_id=:t"),
                                  {"c": cid, "t": tid}).scalar()
    _reset_stats()
    report = await _run(engine, tmp_path, project, suite, tid)
    assert report.verdict == "green", report
    assert _stats()["login_attempts"] == 0, "no mid-suite re-login: the engine never re-logs-in once running"
    with engine.connect() as c:
        seeded_after = c.execute(text("select seeded_at from session where credential_id=:c and target_id=:t"),
                                 {"c": cid, "t": tid}).scalar()
    assert seeded_after == seeded_before, "session re-seeded mid-run — mid-suite re-login leaked in"
    # (b) cooperative cancellation on a gate park (no creds → park, queued tasks cancelled, zero runs)
    proj2 = "plat-coop"
    tid2, suite2, _ = _seed_auth_platform(engine, project=proj2, target_key="coop-form",
                                          auth={"kind": "form", "signin_path": "/login-form",
                                                "creds_ref": "coop-none"})
    sr2 = repo.create_suite_run(engine, project_id=proj2, suite_id=suite2, target_id=tid2)
    report2 = await run_suite(engine, LocalDiskStore(str(tmp_path / "art2")), suite_run_id=sr2,
                              target_id=tid2, max_parallel=1)
    assert report2.verdict == "parked_auth" and report2.results == [], report2
    with engine.connect() as c:
        q = c.execute(text("select count(*) from run_task where suite_run_id=:s and state='queued'"),
                      {"s": sr2}).scalar()
        canc = c.execute(text("select count(*) from run_task where suite_run_id=:s and state='cancelled'"),
                         {"s": sr2}).scalar()
    assert q == 0 and canc >= 1, "gate park did not cooperatively cancel queued tasks"


async def test_bundle_never_contains_creds(engine, vault_key, tmp_path, _clean_cred_env):
    """T23 (AC7): authoring a flow on a login-gated fixture — the session is INJECTED (storage_state),
    credentials are NEVER typed as a step — so the frozen bundle JSON contains neither the fixture
    username nor password. Credentials are engine config, not test content (§3.5)."""
    from anneal_authoring.service import author_and_freeze
    project = "plat-author"
    tid, _, _ = _seed_auth_platform(engine, project=project, target_key="auth-authoring",
                                    auth=_FORM_AUTH, probe_path="/private")
    ss = str(tmp_path / "authoring.json")
    await _make_storage_state(_FORM_AUTH, _FIXTURE_CREDS, ss)          # session-gated surface reached
    cid = repo.ensure_credential_anchor(engine, project_id=project, persona="member")
    repo.upsert_session(engine, credential_id=cid, target_id=tid, storage_state_path=ss, state="valid")
    res = await author_and_freeze(
        engine, project_id=project, test_key="private-smoke",
        nl="Confirm the members-only area renders — the private content heading is visible.",
        target_id=tid, persona="member")
    assert res["status"] == "frozen", f"authoring did not freeze on the login-gated fixture: {res}"
    blob = json.dumps(res["bundle"])
    assert "fixture-user" not in blob and "fixture-pass" not in blob, \
        "credential material leaked into the frozen bundle (AC7 violated)"


# ── Phase E — T24–T25: onboarding endpoint + CLI mirror ──────────────────────────────────────────
async def test_onboard_endpoint(engine, vault_key, tmp_path, _clean_cred_env):
    """T24 (AC1): POST /projects/{p}/targets → a target row with the auth block + an ENCRYPTED
    credential; an immediately-launched suite is green (the lazy first login uses the onboarded cred)."""
    from httpx import ASGITransport, AsyncClient

    from anneal_api.app import create_app
    project = "onb-endpoint"
    _seed_project(engine, project)
    app = create_app(engine=engine, artifacts=LocalDiskStore(str(tmp_path / "art")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/projects/{project}/targets", json={
            "key": "onb-form", "base_url": GOLDEN_URL, "probe_path": "/private",
            "auth": {"kind": "form", "signin_path": "/login-form"},
            "persona": "member", "username": "fixture-user", "password": "fixture-pass"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["credential_stored"] is True
        tid = body["target_id"]
    # target row carries the auth block; the credential is encrypted at rest (no plaintext)
    tgt = repo.get_target(engine, target_id=tid)
    assert tgt["profile"]["auth"]["kind"] == "form"
    with engine.connect() as c:
        raw = bytes(c.execute(text("select secret_enc from credential where project_id=:p and persona='member'"),
                              {"p": project}).scalar_one())
    assert b"fixture-pass" not in raw, "onboarded credential stored in plaintext"
    # seed the flow + suite and launch → green (zero-new-code onboarding, AC1)
    test_id, _ = repo.upsert_test(engine, project_id=project, test_key="auth-private-smoke",
                                  bundle=_auth_bundle(project, "onb-form"), verified_against="onb-form")
    suite = repo.create_suite(engine, project_id=project, name="onb-suite", member_test_ids=[test_id])
    _reset_stats()
    report = await _run(engine, tmp_path, project, suite, tid)
    assert report.verdict == "green", report
    assert _stats()["login_attempts"] == 1, "lazy first login should run exactly once"


async def test_onboard_env_only(engine, tmp_path, monkeypatch, _clean_cred_env):
    """T25 (AC1): the CLI `anneal onboard` (mirror of POST /targets) with NO creds + an env layer set →
    an immediately-launched suite is green (lazy first login resolves creds from the env layer). Drives
    the CLI against a live API server (D32: the CLI forks no behavior)."""
    import socket
    import threading
    import time

    import uvicorn

    from anneal_api.app import create_app
    from anneal_cli.main import main as cli
    project = "onb-env"
    _seed_project(engine, project)
    monkeypatch.setenv("ANNEAL_UI_USERNAME", "fixture-user")   # env layer 3 covers creds (no DB cred)
    monkeypatch.setenv("ANNEAL_UI_PASSWORD", "fixture-pass")
    app = create_app(engine=engine, artifacts=LocalDiskStore(str(tmp_path / "art")))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                httpx.get(base + "/openapi.json", timeout=1); break
            except Exception:
                time.sleep(0.1)
        client = httpx.Client(base_url=base, timeout=180)
        try:
            rc = cli(["onboard", project, "--key", "onb-env-form", "--base-url", GOLDEN_URL,
                      "--kind", "form", "--signin-path", "/login-form", "--probe-path", "/private"],
                     client=client)
            assert rc == 0, "CLI onboard did not succeed"
        finally:
            client.close()
    finally:
        server.should_exit = True; th.join(timeout=10)
    # the CLI created the target; NO credential was stored (env-only onboarding)
    with engine.connect() as c:
        tid = str(c.execute(text("select id from target where project_id=:p and key='onb-env-form'"),
                            {"p": project}).scalar_one())
        ncred = c.execute(text("select count(*) from credential where project_id=:p"), {"p": project}).scalar()
    assert ncred == 0, "env-only onboard must NOT store a credential"
    # launch → green: the lazy first login resolves creds from the env layer
    test_id, _ = repo.upsert_test(engine, project_id=project, test_key="auth-private-smoke",
                                  bundle=_auth_bundle(project, "onb-env-form"), verified_against="onb-env-form")
    suite = repo.create_suite(engine, project_id=project, name="onb-env-suite", member_test_ids=[test_id])
    _reset_stats()
    report = await _run(engine, tmp_path, project, suite, tid)
    assert report.verdict == "green", report
    assert _stats()["login_attempts"] == 1
