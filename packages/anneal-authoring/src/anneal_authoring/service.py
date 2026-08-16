"""HOT-lane orchestration (D24): interview → author → calibrate → freeze → persist a test_version.

draft → calibrated → frozen. An unanswered ambiguity interview blocks the freeze (D26); calibration
must pass (D7) before a bundle is frozen; the destructive guard binds the author lane (D15).
"""
from __future__ import annotations

import json

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from anneal_core.errors import CompileError, LocatorAmbiguous, LocatorUnresolved
from anneal_db import repositories as repo

from .author import AuthoringIncomplete, author_bundle, ungrounded_drops
from .calibrate import CalibrationError, calibrate
from .interview import merge_answers, open_questions


class _NullSink:
    """The golden replay only wants VIDEO — no step_event trail. A sink with no emit_started so the
    interpreter's B2 duck-type check is a no-op."""

    def emit(self, run_result_id, event):
        pass

# A genuine authoring MISS — a stochastic author couldn't ground a step, resolve a control, satisfy the
# D7 falsifiability floor, or the page timed out. These are retried (A, outcome-determinism, mirrors the
# red path's B1). NOT retried: an unanswered ambiguity (needs_answers, handled by the interview BEFORE
# authoring) and engine faults (they surface loud, never here). A retry re-authors from scratch; it never
# relaxes calibration and never touches a replay verdict.
_AUTHOR_MISS = (AuthoringIncomplete, LocatorUnresolved, LocatorAmbiguous, CompileError, CalibrationError,
                PlaywrightError, PlaywrightTimeout, json.JSONDecodeError)


def _start_path(target: dict) -> str:
    return (target.get("profile") or {}).get("probe_path") or "/"


async def _author_once(engine, *, target, start_path, full_nl, project_id, test_key, persona,
                       target_id, storage_state, model):
    """One author→calibrate walk. Raises an _AUTHOR_MISS on a stochastic miss (retried by the caller)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            ctx = await browser.new_context(base_url=target["base_url"], storage_state=storage_state,
                                            viewport={"width": 1280, "height": 720})
            page = await ctx.new_page()
            result = await author_bundle(page, nl=full_nl, project_id=project_id, test_id=test_key,
                                         persona=persona, target_key=target["key"], start_path=start_path,
                                         model=model)
            await ctx.close()
            bundle = result.bundle
            if result.territory:                              # D25: persist recon as TerritoryNotes
                repo.upsert_territory_notes(engine, target_id=target_id, notes=result.territory)
            # ── D7 calibration → freeze (D24) — same storage_state as authoring (auth parity) ──
            calib = await calibrate(browser, target["base_url"], bundle, start_path,
                                    storage_state=storage_state)
        finally:
            await browser.close()
    return bundle, calib, result.skipped_steps


async def record_golden(engine, artifacts, *, test_id, project_id, target, bundle, version,
                        start_path, storage_state) -> bool:
    """B4: auto-seed the per-test golden — a CLEAN cold replay of the just-frozen bundle recorded to
    video (the canonical 'how it should look', better than the messy authoring walk). Best-effort: a
    replay error just skips the seed (a reviewer can still upload). The video lands directly in the
    golden slot `golden/{test_id}` so no copy is needed. Returns True iff a golden was recorded."""
    from datetime import date

    from anneal_core.bundle import Bundle
    from anneal_core.compile import compile as compile_bundle
    from anneal_core.stepplan import CompileContext
    from anneal_db.engine import mint_token
    from anneal_executor.evidence import recording
    from anneal_executor.interpreter import Interpreter
    from playwright.async_api import async_playwright

    tok = mint_token()
    modules = repo.load_modules(engine, project_id=project_id)
    ctx = CompileContext(token=tok, today=date.today(),
                         date_format=target.get("date_format") or "%Y-%m-%d", params={}, modules=modules)
    try:
        plan = compile_bundle(Bundle.model_validate(bundle), ctx)
        async with async_playwright() as p:
            br = await p.chromium.launch()
            try:
                async with recording(br, artifacts, f"golden/{test_id}", base_url=target["base_url"],
                                     storage_state=storage_state) as (page, ev):
                    await Interpreter.cold(page, plan, _NullSink(), artifacts, token=tok,
                                           run_result_id=f"golden-{test_id}",
                                           video_epoch=ev.video_epoch).run()
            finally:
                await br.close()
    except Exception:  # noqa: BLE001 — the golden is a nicety; never fail a freeze on it
        import traceback
        traceback.print_exc()
        return False
    if not ev.video_path:
        return False
    repo.set_golden(engine, test_id=test_id, video_path=ev.video_path, source="auto",
                    reflects_version=version)
    return True


async def _ensure_session(engine, *, target, target_id, project_id, persona):
    """Seed a session for an auth-configured target that has none yet (config-only login at authoring
    time, §3.5). Returns the storage_state path, or None (no auth block / no resolvable creds / hard-
    auth signal → authoring proceeds without a session, i.e. unchanged behavior)."""
    import os
    import pathlib

    from playwright.async_api import async_playwright

    from anneal_db import vault
    from anneal_executor.login import login

    auth = (target.get("profile") or {}).get("auth") or {}
    if auth.get("kind", "none") == "none":
        return None
    creds_ref = auth.get("creds_ref") or project_id
    try:
        creds = vault.resolve(engine, creds_ref=creds_ref, project_id=project_id, persona=persona)
    except (vault.CredentialsNotFound, vault.VaultKeyMissing):
        return None                                          # no creds anywhere → author unauth (as before)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            ctx = await browser.new_context()
            outcome = await login(await ctx.new_page(), auth, creds, base_url=target["base_url"])
            if not outcome.is_ok:                            # bad creds / captcha-OTP-SSO → give up quietly
                return None
            d = pathlib.Path(os.getenv("ANNEAL_ARTIFACTS", "./.anneal/artifacts")) / "auth" / project_id
            d.mkdir(parents=True, exist_ok=True)
            path = str(d / f"{target['key']}-{persona}.json")
            await ctx.storage_state(path=path)
        finally:
            await browser.close()
    cred_id = repo.ensure_credential_anchor(engine, project_id=project_id, persona=persona)
    repo.upsert_session(engine, credential_id=cred_id, target_id=target_id,
                        storage_state_path=path, state="valid")
    return path


async def author_and_freeze(engine, *, project_id, test_key, nl, target_id, persona="member",
                            answers=None, model=None, max_attempts=4, artifacts=None) -> dict:
    target = repo.get_target(engine, target_id=target_id)
    start_path = _start_path(target)

    # ── D26 interview: unanswered ambiguity blocks the freeze (never guessed) ──
    # only a non-blank answer counts — a blank one must NOT suppress its question in code, so the
    # interview can never be unblocked by an empty/garbage answer (GOAL rule 6).
    answers = {q: a for q, a in (answers or {}).items() if isinstance(a, str) and a.strip()}
    questions = open_questions(nl, answers, model=model)
    if questions:
        return {"status": "needs_answers", "questions": questions}

    full_nl = merge_answers(nl, answers)
    # multi-platform (§3.5): resolve the session for THIS target — sessions are (credential_id,
    # target_id)-keyed, so omitting target_id returns the persona's most-recently-validated session for
    # ANY target, which on a multi-target project loads the WRONG platform's (or a stale) session and
    # authors unauthenticated. get_storage_state's own contract says pass target_id here.
    storage_state = repo.get_storage_state(engine, project_id=project_id, persona=persona,
                                           target_id=target_id)
    if storage_state is None:
        # Config-only login (§3.5) applied at AUTHORING time: a freshly-onboarded login-gated target
        # has creds but no seeded session yet, so authoring would land on the sign-in page. Establish
        # the session ONCE via the engine strategy (reuse-first thereafter). No auth / no creds /
        # hard-auth → None, and authoring proceeds as before (unchanged for kind:none targets).
        storage_state = await _ensure_session(engine, target=target, target_id=target_id,
                                              project_id=project_id, persona=persona)

    # ── A (outcome-determinism, mirrors B1): retry a stochastic author MISS to a real frozen+calibrated
    #    bundle. Every attempt still authors a working flow and must pass D7 calibration — nothing is
    #    relaxed. On budget exhaustion the last real miss re-raises (honest failure, not a false freeze).
    last_exc = None
    skipped_steps: list[dict] = []
    for _ in range(max_attempts):
        try:
            bundle, calib, skipped_steps = await _author_once(
                engine, target=target, start_path=start_path, full_nl=full_nl, project_id=project_id,
                test_key=test_key, persona=persona, target_id=target_id, storage_state=storage_state,
                model=model)
            # ── freeze coverage gate (RC4 at the freeze boundary) ──
            # The engine RECORDS every step it could not ground (skipped_steps), but calibration only
            # ever sees the SURVIVING steps — so a flow that lost its intended acts (a create/choose/
            # cancel the author failed to ground) can still pass D7 falsifiability + the positive replay
            # and freeze GREEN as a hollow shell. Refuse that here: a dropped intended interaction is an
            # _AUTHOR_MISS like any other, so it RE-AUTHORS (a stochastic miss may ground next attempt)
            # and, on budget exhaustion, re-raises as an honest failure — never a false freeze. The
            # engine's DELIBERATE over-plan reductions (kind == over_plan) are benign and pass through,
            # surfaced to the caller on the frozen response.
            dropped = ungrounded_drops(skipped_steps)
            if dropped:
                raise AuthoringIncomplete(
                    f"freeze coverage gate: the author could not ground {len(dropped)} intended step(s) "
                    f"and dropped them — refusing to freeze a bundle that silently omits them (a hollow "
                    f"green). Dropped: {[s['intent'] for s in dropped]}")
            break
        except _AUTHOR_MISS as exc:
            last_exc = exc
    else:
        raise last_exc

    bundle["state"] = "frozen"
    bundle["calibration"] = calib
    bundle["lineage"]["ruling"] = "canonical"
    test_id, version = repo.upsert_test(engine, project_id=project_id, test_key=test_key,
                                        bundle=bundle, verified_against=target["key"])
    # B4: auto-seed the golden from a clean replay (API path passes artifacts; the m2/direct path
    # leaves it None → no golden, no extra replay, behavior unchanged).
    if artifacts is not None:
        await record_golden(engine, artifacts, test_id=test_id, project_id=project_id, target=target,
                            bundle=bundle, version=version, start_path=start_path,
                            storage_state=storage_state)
    return {"status": "frozen", "test_id": test_id, "version": version,
            "calibration": calib, "steps": len(bundle["steps"]), "bundle": bundle,
            "skipped_steps": skipped_steps}
