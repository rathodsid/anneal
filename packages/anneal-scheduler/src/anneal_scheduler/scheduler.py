"""The scheduler + fleet (§10, D34, C-3). v1 laptop compose: all workers share one process; a worker
is an async task holding a browser context seeded from the persona's storage state (contexts cheap,
sessions scarce — HLD §8).

M1 scope: the §9 probe gate (validate the SEEDED session and park AUTH_EXPIRED before any run_task is
claimed), concurrent SKIP-LOCKED claim (3.1d) + cold replay per task recording run_result + live
step_events (D33), the D29 suite verdict, and the retention prune (D12, in repositories). The exclusive
advisory lock (3.1c) + name-pattern sweeper are M4.

Note (honesty): at v1 laptop scale the DB calls are sync, so claims serialize; concurrency is real for
the browser-bound portion of each run (they interleave at every await), not for the DB claim itself.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from anneal_classify import redpath
from anneal_core.bundle import Bundle
from anneal_core.compile import compile as compile_bundle
from anneal_core.errors import classify_exception
from anneal_core.stepplan import CompileContext
from anneal_db import repositories as repo
from anneal_db.sink import DBStepEventSink
from anneal_executor.evidence import recording
from anneal_executor.interpreter import Interpreter
from anneal_executor.session import probe_session

from .sweeper import sweep_suite_run

# probe-state → (suite verdict, park_reason). Env/auth are distinct so a degraded environment never
# reads as a product failure (§9/§10).
_PARK = {"expired": ("parked_auth", "AUTH_EXPIRED"),
         "env_capacity": ("parked_env", "ENV_CAPACITY"),
         "env_infra": ("parked_env", "ENV_INFRA")}


async def _acquire_exclusive(engine, target_key, *, shared=False):
    """3.1c — the SESSION-scoped advisory lock on a dedicated connection, held for the whole run.
    Extends LLD §3.1c: an exclusive test takes the key EXCLUSIVELY and every ordinary test takes the
    SAME key in SHARED mode, so ordinary tests still run concurrently with each other but never
    alongside an exclusive one (shared, non-namespaceable state is actually never raced). Polls with
    pg_try_advisory_lock[_shared] + `await sleep` so a WAIT yields to other workers (each poll's sync
    `conn.execute` still momentarily blocks, per the codebase's laptop-scale sync-SQL pattern)."""
    import asyncio

    from sqlalchemy import text
    key = f"{target_key}:exclusive"            # build the lock key in Python — a literal ':exclusive'
    conn = engine.connect()                    # inside text() would be read as a bind parameter
    fn = "pg_try_advisory_lock_shared" if shared else "pg_try_advisory_lock"
    while True:
        got = conn.execute(text(f"select {fn}(hashtextextended(:k, 0))"), {"k": key}).scalar()
        conn.commit()                          # end the implicit txn; the session-level lock persists
        if got:
            return conn
        await asyncio.sleep(0.05)


def _release_exclusive(conn, target_key, *, shared=False):
    from sqlalchemy import text
    fn = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
    try:
        conn.execute(text(f"select {fn}(hashtextextended(:k, 0))"), {"k": f"{target_key}:exclusive"})
        conn.commit()
    finally:
        conn.close()


@dataclass
class SuiteRunReport:
    suite_run_id: str
    state: str                 # done | parked
    verdict: str               # green | red | parked_auth | parked_env
    park_reason: str | None = None
    results: list[dict] | None = None
    auth_reason: str | None = None   # verbatim soft-relogin reason (§3.5) — machine code stays park_reason
    needs_human: bool = False        # True only on a hard-auth signal (captcha/OTP/SSO, G5)


@dataclass
class _Relogin:
    ok: bool
    needs_human: bool = False
    reason: str | None = None


def _park(engine, suite_run_id, verdict, reason, *, auth_reason=None, needs_human=False):
    """Cooperative park (§9): cancel queued tasks (nothing else runs) + persist the parked state.
    `park_reason` stays the machine code (e.g. AUTH_EXPIRED); the verbatim human reason rides on
    `auth_reason` so a downstream consumer keying on the code is unaffected."""
    repo.cancel_queued_tasks(engine, suite_run_id=suite_run_id)
    repo.set_suite_run_state(engine, suite_run_id=suite_run_id, state="parked",
                             verdict=verdict, park_reason=reason,
                             auth_reason=auth_reason, needs_human=needs_human)
    return SuiteRunReport(suite_run_id, "parked", verdict, reason, results=[],
                          auth_reason=auth_reason, needs_human=needs_human)


async def _probe(browser, target, probe_path, storage_state):
    ctx = await browser.new_context(storage_state=storage_state)
    try:
        return await probe_session(await ctx.new_page(), target["base_url"], probe_path=probe_path)
    finally:
        await ctx.close()


async def _soft_relogin(engine, artifacts, browser, target, target_id, persona, auth_cfg) -> _Relogin:
    """§3.5 / D14.1 soft re-login — ONE attempt at the probe gate. Resolve creds (§3.2), run the
    provider strategy once, and on success persist the storage_state + update the session row. A hard
    signal (captcha/OTP/SSO) → needs_human; a plain failure → honest reason; no retry loop. Missing
    creds / a refused vault read → an honest reason (never a crash into a park)."""
    from anneal_db import vault
    from anneal_executor.login import login

    creds_ref = auth_cfg.get("creds_ref") or target["project_id"]
    try:
        creds = vault.resolve(engine, creds_ref=creds_ref, project_id=target["project_id"],
                              persona=persona)
    except (vault.CredentialsNotFound, vault.VaultKeyMissing) as e:
        return _Relogin(False, False, str(e))       # expected auth outcome → honest reason, not a crash

    ctx = await browser.new_context()
    try:
        outcome = await login(await ctx.new_page(), auth_cfg, creds, base_url=target["base_url"])
        if outcome.needs_human:
            return _Relogin(False, True, outcome.reason)
        if not outcome.is_ok:
            return _Relogin(False, False, outcome.reason)
        # ok → persist the reusable storage_state under the auth namespace, then update the session row
        path = artifacts.path_for(f"auth/{target['project_id']}", f"{target['key']}-{persona}.json")
        await ctx.storage_state(path=path)
    finally:
        await ctx.close()
    cred_id = repo.ensure_credential_anchor(engine, project_id=target["project_id"], persona=persona)
    repo.upsert_session(engine, credential_id=cred_id, target_id=target_id,
                        storage_state_path=path, state="valid")
    return _Relogin(True)


def _today(tz: str):
    try:
        return datetime.now(ZoneInfo(tz)).date()
    except Exception:
        return datetime.now(ZoneInfo("UTC")).date()


def _probe_path(target: dict) -> str:
    # a target FACT — lives in target.profile (TargetProfile/D25), never hardcoded in the engine.
    return (target.get("profile") or {}).get("probe_path") or "/"


async def _ensure_step_events_visible(engine, run_result_id, expected) -> int:
    """D33 evidence integrity: a completed run persists one step_event per executed step. Read the
    trail back with a short bounded wait so a run's verdict is never finalized until its events are
    durably VISIBLE (the sync sink commits inline, so this is normally satisfied on the first read;
    the retry is defence for any lag). Returns the observed count."""
    import asyncio

    seen = 0
    for _ in range(10):                          # ~0.5s ceiling; returns immediately once complete
        seen = repo.count_step_events(engine, run_result_id=run_result_id)
        if seen >= expected:
            return seen
        await asyncio.sleep(0.05)
    return seen


def _finalize_with_evidence(verdict, error_class, seen, expected):
    """A PASS-class verdict must not finalize over an incomplete trail: classification and the console
    read step_event rows, so a green with fewer persisted events than steps executed is an engine
    integrity fault (the engine's fault, never the app's), not a silent pass. Pure → unit-testable."""
    if verdict in ("green", "green_with_heals", "provisional") and seen < expected:
        return "red", "EVIDENCE_INCOMPLETE"
    return verdict, error_class


async def _run_one(engine, artifacts, browser, target, modules, claim) -> dict:
    """Compile + cold-replay a single claimed run_task. A run_result row is created up front, so a
    setup-time failure (bad bundle, browser/context error) still records a terminal verdict instead of
    leaving the suite dangling in 'running'."""
    rr_id = repo.start_run_result(engine, run_task_id=claim["run_task_id"], test_id=claim["test_id"],
                                  test_version=claim["test_version"], environment=target["key"])
    sink = DBStepEventSink(engine)
    started = time.monotonic()

    # ── stage 1: compile + cold replay. A setup/cold failure finalizes the run (never dangles). ──
    try:
        bundle_dict = repo.load_bundle(engine, test_id=claim["test_id"], version=claim["test_version"])
        bundle = Bundle.model_validate(bundle_dict)
        storage_state = repo.get_storage_state(engine, project_id=target["project_id"],
                                               persona=bundle.persona, target_id=target["id"])
        ctx = CompileContext(token=claim["token"], today=_today(target["timezone"]),
                             date_format=target["date_format"], params={}, modules=modules)
        plan = compile_bundle(bundle, ctx)
        async with recording(browser, artifacts, rr_id, base_url=target["base_url"],
                             storage_state=storage_state) as (page, ev):
            outcome = await Interpreter.cold(page, plan, sink, artifacts, token=claim["token"],
                                             run_result_id=rr_id, video_epoch=ev.video_epoch).run()
        trace_path, video_path, screenshot_path = ev.trace_path, ev.video_path, ev.screenshot_path
        network_log_path = ev.network_log_path
    except Exception as exc:  # noqa: BLE001 — a setup failure must still finalize the run/suite
        ec = classify_exception(exc, "compile").value
        duration_ms = int((time.monotonic() - started) * 1000)
        repo.finish_run_result(engine, run_result_id=rr_id, run_task_id=claim["run_task_id"],
                               test_id=claim["test_id"], verdict="red", error_class=ec,
                               duration_ms=duration_ms)
        return {"run_result_id": rr_id, "test_id": claim["test_id"], "verdict": "red",
                "error_class": ec, "duration_ms": duration_ms}

    # ── stage 2: red path (§6/§9) under its OWN error boundary. An orchestration bug is an ENGINE
    #    fault (the engine's fault, never the app's) — it must never masquerade as a test verdict. ──
    try:
        if outcome.verdict == "green":
            verdict = redpath.on_green_run(engine, project_id=target["project_id"], target=target,
                                           test_id=claim["test_id"], run_result_id=rr_id, plan=plan,
                                           bundle=bundle_dict)
            error_class = None
        else:
            # §6: one automatic rerun — a REAL run_result (B3: role='retry', own artifact dir + trail)
            retry_id = repo.start_child_run(engine, parent_run_id=rr_id, role="retry")
            retry_started = time.monotonic()
            async with recording(browser, artifacts, retry_id, base_url=target["base_url"],
                                 storage_state=storage_state) as (page2, ev2):
                retry = await Interpreter.cold(page2, plan, sink, artifacts,
                                               token=claim["token"], run_result_id=retry_id,
                                               video_epoch=ev2.video_epoch).run()
            repo.finish_child_run(engine, run_result_id=retry_id, verdict=retry.verdict,
                                  error_class=retry.error_class, trace_path=ev2.trace_path,
                                  video_path=ev2.video_path, screenshot_path=ev2.screenshot_path,
                                  network_log_path=ev2.network_log_path,
                                  duration_ms=int((time.monotonic() - retry_started) * 1000))
            if retry.verdict == "green":
                verdict, error_class = "green", "FLAKY"          # pass-on-retry = FLAKY (opens nothing)
            else:
                verdict = await redpath.on_red_run(
                    engine, artifacts, project_id=target["project_id"], target=target,
                    test_id=claim["test_id"], run_result_id=rr_id, outcome=outcome, plan=plan,
                    bundle=bundle_dict, token=claim["token"], modules=modules, persona=bundle.persona)
                error_class = outcome.error_class
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        verdict, error_class = "red", "ENGINE_FAULT"

    # ── D33 evidence-trail integrity: don't finalize a PASS until its step_events are durably visible ──
    if verdict in ("green", "green_with_heals", "provisional"):
        seen = await _ensure_step_events_visible(engine, rr_id, outcome.steps_run)
        verdict, error_class = _finalize_with_evidence(verdict, error_class, seen, outcome.steps_run)

    duration_ms = int((time.monotonic() - started) * 1000)
    repo.finish_run_result(engine, run_result_id=rr_id, run_task_id=claim["run_task_id"],
                           test_id=claim["test_id"], verdict=verdict, error_class=error_class,
                           trace_path=trace_path, video_path=video_path, screenshot_path=screenshot_path,
                           network_log_path=network_log_path, duration_ms=duration_ms)
    return {"run_result_id": rr_id, "test_id": claim["test_id"], "verdict": verdict,
            "error_class": error_class, "duration_ms": duration_ms}


async def run_suite(engine, artifacts, *, suite_run_id, target_id, max_parallel=4) -> SuiteRunReport:
    import asyncio

    from playwright.async_api import async_playwright

    target = repo.get_target(engine, target_id=target_id)
    modules = repo.load_modules(engine, project_id=target["project_id"])
    parallel = min(max_parallel, target.get("max_parallel") or max_parallel)

    n_members = repo.expand_run_tasks(engine, suite_run_id=suite_run_id)
    if n_members == 0:                          # an empty suite must NOT pass a deploy gate vacuously
        repo.set_suite_run_state(engine, suite_run_id=suite_run_id, state="parked",
                                 verdict="parked_env", park_reason="EMPTY_SUITE")
        return SuiteRunReport(suite_run_id, "parked", "parked_env", "EMPTY_SUITE", results=[])

    repo.set_suite_run_state(engine, suite_run_id=suite_run_id, state="probing")
    personas = repo.suite_member_personas(engine, suite_run_id=suite_run_id) or [None]
    probe_path = _probe_path(target)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            # ── §9/§10 probe gate: validate each persona's SEEDED session + env health BEFORE any claim.
            #    On auth expiry, §3.5/D14.1: ONE soft re-login attempt (reuse-first, honest parks). ──
            for persona in personas:
                ss = (repo.get_storage_state(engine, project_id=target["project_id"], persona=persona,
                                             target_id=target_id) if persona else None)
                state = await _probe(browser, target, probe_path, ss)
                if state == "valid":
                    continue                          # reuse the seeded session — no login ever ran (G4)
                if state in ("env_capacity", "env_infra"):
                    verdict, reason = _PARK[state]    # degraded env → park distinctly, never as auth
                    return _park(engine, suite_run_id, verdict, reason)

                # state == "expired"
                auth_cfg = (target.get("profile") or {}).get("auth") or {"kind": "none"}
                if not persona or auth_cfg.get("kind", "none") == "none":
                    return _park(engine, suite_run_id, *_PARK["expired"])   # no in-engine login (D14)

                relogin = await _soft_relogin(engine, artifacts, browser, target, target_id, persona,
                                              auth_cfg)
                if not relogin.ok:                    # hard-auth / bad creds / no creds → honest park (G5)
                    return _park(engine, suite_run_id, *_PARK["expired"],
                                 auth_reason=relogin.reason, needs_human=relogin.needs_human)
                # re-save happened; re-probe the freshly-seeded session before running the suite
                ss2 = repo.get_storage_state(engine, project_id=target["project_id"], persona=persona,
                                             target_id=target_id)
                if await _probe(browser, target, probe_path, ss2) != "valid":
                    return _park(engine, suite_run_id, *_PARK["expired"],
                                 auth_reason="soft re-login reported success but the session still "
                                             "probes as expired")

            repo.set_suite_run_state(engine, suite_run_id=suite_run_id, state="running")
            results: list[dict] = []

            async def worker(wid: str):
                while True:
                    claim = repo.claim_next(engine, suite_run_id=suite_run_id, worker_id=wid)
                    if claim is None:
                        return
                    # §8/3.1c: an exclusive test holds the target's advisory lock exclusively for its
                    # whole run; an ordinary test holds it SHARED — so ordinary tests interleave freely
                    # but none ever runs alongside an exclusive one (see _acquire_exclusive).
                    shared = not claim["exclusive"]
                    lock = await _acquire_exclusive(engine, target["key"], shared=shared)
                    try:
                        results.append(await _run_one(engine, artifacts, browser, target, modules, claim))
                    finally:
                        _release_exclusive(lock, target["key"], shared=shared)

            await asyncio.gather(*[worker(f"w{i}") for i in range(parallel)])

            # ── §10.2 suite-end sweeper: deterministic residue cleanup (A16, D15) ──
            # replay deletes through the run's OWN seeded session so an auth-gated, workspace-scoped
            # live app accepts them (a bare unauthenticated client cannot — it left live residue).
            try:
                # single-session assumption: sweep under the first persona's session. Sufficient for a
                # single-persona suite (the corpus); a multi-persona suite whose entities span different
                # workspaces would need a per-persona sweep — a follow-up, not a regression (the previous
                # unauthenticated client swept ZERO workspace-scoped residue).
                sweep_persona = next((p for p in personas if p), None)
                sweep_ss = (repo.get_storage_state(engine, project_id=target["project_id"],
                                                   persona=sweep_persona, target_id=target["id"])
                            if sweep_persona else None)
                await sweep_suite_run(engine, suite_run_id=suite_run_id, target=target,
                                      browser=browser, storage_state=sweep_ss)
            except Exception:  # noqa: BLE001 — the sweeper is a safety net; never fail the suite on it
                import traceback
                traceback.print_exc()
        finally:
            await browser.close()

    # D29 verdict precedence: a real red dominates; provisional counts as pass (annotated).
    vs = {r["verdict"] for r in results}
    verdict = next((p for p in ("red", "parked_auth", "parked_env", "provisional",
                                "green_with_heals", "green") if p in vs), "green")
    repo.set_suite_run_state(engine, suite_run_id=suite_run_id, state="done", verdict=verdict)
    return SuiteRunReport(suite_run_id, "done", verdict, results=results)
