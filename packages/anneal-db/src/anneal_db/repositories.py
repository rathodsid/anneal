"""Repositories (§3) — parameterized Core SQL. Implements the runtime concurrency primitives:
3.1d SKIP-LOCKED claim + token mint (here), 3.1b atomic budget guard + 3.1c advisory lock (M3/M4).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .engine import mint_token


def _now():
    return datetime.now(timezone.utc)


# ── seeding (a target + a frozen read-only test + a suite) ───────────────────
def seed_project_target(engine, *, project_id, project_name, target_key, base_url, timezone_="UTC",
                        profile=None):
    with engine.begin() as c:
        c.execute(text("insert into project (id, name) values (:i,:n) on conflict (id) do nothing"),
                  {"i": project_id, "n": project_name})
        row = c.execute(text("""
            insert into target (project_id, key, base_url, timezone, profile)
            values (:p,:k,:u,:tz, cast(:pf as jsonb))
            on conflict (project_id, key)
              do update set base_url = excluded.base_url, profile = excluded.profile
            returning id"""),
            {"p": project_id, "k": target_key, "u": base_url, "tz": timezone_,
             "pf": json.dumps(profile or {})}).one()
        return str(row.id)


def get_storage_state(engine, *, project_id, persona, target_id=None):
    """A10/D14: the seeded storage-state ref for a persona's VALID session, or None. Injecting this
    (no credential decryption) is what makes the §9 probe validate the *seeded* session, not an
    anonymous visit.

    Sessions are now keyed `(credential_id, target_id)` (design §3.5), so pass `target_id` to get the
    persona's session for THIS platform (the correct multi-platform lookup). `target_id=None` keeps the
    pre-multi-platform behavior (the persona's most-recently-validated valid session, any target) for
    the single-target callers that predate the keying change."""
    with engine.connect() as c:
        if target_id is not None:
            row = c.execute(text("""
                select s.storage_state_path
                from session s join credential c on c.id = s.credential_id
                where c.project_id = :p and c.persona = :persona and s.target_id = :t
                  and s.state = 'valid'"""),
                {"p": project_id, "persona": persona, "t": target_id}).first()
        else:
            row = c.execute(text("""
                select s.storage_state_path
                from session s join credential c on c.id = s.credential_id
                where c.project_id = :p and c.persona = :persona and s.state = 'valid'
                order by s.last_validated desc nulls last limit 1"""),
                {"p": project_id, "persona": persona}).first()
        if not row:
            return None
        # The row stores a filesystem PATH; the DB (persistent/managed) and the artifact store can
        # diverge — a fresh/reset artifacts volume, a different host, or a pruned artifact leaves the
        # session row pointing at a file that isn't materialized HERE. A dangling path is not a usable
        # session: degrade to None (the well-handled "no session" case every lane recovers from —
        # config-only re-login / proceed unauth), never hand a dead path to browser.new_context (which
        # raises FileNotFoundError and crashes the lane). Target-agnostic; no app specifics.
        path = row.storage_state_path
        return path if path and Path(path).exists() else None


def ensure_credential_anchor(engine, *, project_id, persona) -> str:
    """Return the credential id for `(project_id, persona)`, creating a session-ANCHOR row if none
    exists. Real encrypted secrets are written by `vault.store_credential`; this only guarantees a
    `credential_id` to key the session on when the resolved creds came from the env layers (§3.2 layers
    2–3, no DB credential). The placeholder `secret_enc` is never decrypted."""
    with engine.begin() as c:
        cid = c.execute(text("select id from credential where project_id=:p and persona=:pa"),
                        {"p": project_id, "pa": persona}).scalar()
        if cid is None:
            cid = c.execute(text("insert into credential (project_id, persona, secret_enc) "
                                 "values (:p,:pa,:s) returning id"),
                            {"p": project_id, "pa": persona,
                             "s": b"session-anchor-no-secret"}).scalar()
        return str(cid)


def upsert_session(engine, *, credential_id, target_id, storage_state_path, state="valid"):
    """Insert/refresh the `(credential_id, target_id)` session row after a soft re-login (§3.5): the
    storage_state path is re-pointed and `seeded_at`/`last_validated` bumped to now. One row per
    persona × platform, so re-seeding target B never clobbers target A's session (AC6)."""
    with engine.begin() as c:
        c.execute(text("""
            insert into session (credential_id, target_id, storage_state_path, seeded_at,
                                 last_validated, state)
            values (:c,:t,:sp, now(), now(), :st)
            on conflict (credential_id, target_id) do update set
              storage_state_path = excluded.storage_state_path,
              seeded_at = now(), last_validated = now(), state = excluded.state"""),
            {"c": credential_id, "t": target_id, "sp": storage_state_path, "st": state})


def suite_member_personas(engine, *, suite_run_id):
    """Distinct personas the suite's member tests bind (D20) — the §9 probe checks each."""
    with engine.connect() as c:
        rows = c.execute(text("""
            select distinct tv.bundle->>'persona' as persona
            from suite_run sr
            join suite s on s.id = sr.suite_id
            join test t on t.id = any(s.member_test_ids)
            join test_version tv on tv.test_id = t.id and tv.version = t.head_version
            where sr.id = :sr and tv.bundle->>'persona' is not null"""),
            {"sr": suite_run_id}).all()
        return [r.persona for r in rows]


def upsert_test(engine, *, project_id, test_key, bundle: dict, verified_against):
    """Insert a test + its v1 frozen test_version (idempotent). Returns (test_id, version)."""
    with engine.begin() as c:
        tid = c.execute(text("""
            insert into test (project_id, test_key, head_version) values (:p,:k,1)
            on conflict (project_id, test_key) do update set test_key = excluded.test_key
            returning id"""), {"p": project_id, "k": test_key}).one().id
        exists = c.execute(text("select 1 from test_version where test_id=:t and version=1"),
                           {"t": tid}).first()
        if not exists:
            c.execute(text("""
                insert into test_version (test_id, version, bundle, state, reason, verified_against)
                values (:t, 1, cast(:b as jsonb), 'frozen', 'authored', :va)"""),
                {"t": tid, "b": json.dumps(bundle), "va": verified_against})
        return str(tid), 1


def upsert_module(engine, *, project_id, ref, version, steps: list[dict]):
    """Seed a module + its frozen module_version (D19). Returns module_id."""
    with engine.begin() as c:
        mid = c.execute(text("""
            insert into module (project_id, ref, head_version) values (:p,:r,:v)
            on conflict (project_id, ref) do update set head_version = greatest(module.head_version, :v)
            returning id"""), {"p": project_id, "r": ref, "v": version}).one().id
        exists = c.execute(text("select 1 from module_version where module_id=:m and version=:v"),
                           {"m": mid, "v": version}).first()
        if not exists:
            c.execute(text("""
                insert into module_version (module_id, version, steps, lineage, state)
                values (:m,:v, cast(:s as jsonb), cast('{}' as jsonb), 'frozen')"""),
                {"m": mid, "v": version, "s": json.dumps(steps)})
        return str(mid)


def load_modules(engine, *, project_id):
    """All (ref, version) → steps for a project, for CompileContext.modules."""
    with engine.connect() as c:
        rows = c.execute(text("""
            select m.ref, mv.version, mv.steps
            from module m join module_version mv on mv.module_id = m.id
            where m.project_id = :p"""), {"p": project_id}).all()
        return {(r.ref, r.version): r.steps for r in rows}


def upsert_territory_notes(engine, *, target_id, notes: dict, authored_by="recon"):
    """D25: versioned TerritoryNotes from recon (routes, widget idioms, conventions)."""
    with engine.begin() as c:
        nxt = c.execute(text("select coalesce(max(version),0)+1 as v from territory_notes where target_id=:t"),
                        {"t": target_id}).one().v
        c.execute(text("""insert into territory_notes (target_id, version, notes, authored_by)
                          values (:t,:v, cast(:n as jsonb), :by)"""),
                  {"t": target_id, "v": nxt, "n": json.dumps(notes), "by": authored_by})
        return nxt


def get_target(engine, *, target_id):
    with engine.connect() as c:
        r = c.execute(text("select id, project_id, key, base_url, timezone, date_format, max_parallel, "
                           "profile from target where id = :t"), {"t": target_id}).mappings().one()
        return dict(r)


def create_suite(engine, *, project_id, name, member_test_ids):
    with engine.begin() as c:
        row = c.execute(text("""
            insert into suite (project_id, name, member_test_ids) values (:p,:n,:m)
            on conflict (project_id, name) do update set member_test_ids = excluded.member_test_ids
            returning id"""), {"p": project_id, "n": name, "m": member_test_ids}).one()
        return str(row.id)


def update_suite(engine, *, suite_id, name=None, member_test_ids=None) -> bool:
    """A4: rename / repoint membership (the suite.html membership modal). Returns False if missing."""
    sets, params = [], {"s": suite_id}
    if name is not None:
        sets.append("name = :n"); params["n"] = name
    if member_test_ids is not None:
        sets.append("member_test_ids = :m"); params["m"] = member_test_ids
    if not sets:
        return get_suite(engine, suite_id=suite_id) is not None
    with engine.begin() as c:
        return c.execute(text(f"update suite set {', '.join(sets)} where id = :s"),
                         params).rowcount > 0


def set_pinned(engine, *, run_result_id, pinned) -> bool:
    """A4: reviewer pin/unpin — keep specific evidence beyond the D12 auto-pin of the latest run."""
    with engine.begin() as c:
        return c.execute(text("update run_result set pinned = :pin where id = :r"),
                         {"pin": pinned, "r": run_result_id}).rowcount > 0


# ── run lifecycle ────────────────────────────────────────────────────────────
def create_suite_run(engine, *, project_id, suite_id, target_id, mode="saved", trigger="manual"):
    with engine.begin() as c:
        row = c.execute(text("""
            insert into suite_run (project_id, suite_id, mode, trigger, target_id, state)
            values (:p,:s,:m,:t,:tg,'pending') returning id"""),
            {"p": project_id, "s": suite_id, "m": mode, "t": trigger, "tg": target_id}).one()
        return str(row.id)


def expand_run_tasks(engine, *, suite_run_id):
    """One run_task per suite member test, at the test's head_version."""
    with engine.begin() as c:
        members = c.execute(text("""
            select unnest(s.member_test_ids) as test_id
            from suite_run sr join suite s on s.id = sr.suite_id where sr.id = :sr"""),
            {"sr": suite_run_id}).all()
        for m in members:
            hv = c.execute(text("select head_version from test where id=:t"), {"t": m.test_id}).one().head_version
            exclusive = c.execute(text("""
                select coalesce((bundle->'policy'->>'exclusive')::boolean, false) as ex
                from test_version where test_id=:t and version=:v"""),
                {"t": m.test_id, "v": hv}).one().ex
            c.execute(text("""
                insert into run_task (suite_run_id, test_id, test_version, exclusive, state)
                values (:sr,:t,:v,:ex,'queued')
                on conflict (suite_run_id, test_id) do nothing"""),
                {"sr": suite_run_id, "t": m.test_id, "v": hv, "ex": exclusive})
        return len(members)


def claim_next(engine, *, suite_run_id, worker_id):
    """3.1d — SKIP LOCKED claim + token mint. Returns the claimed row (dict) or None if drained.
    unique(token) makes a rare collision a re-mint retry, not application logic."""
    for _ in range(4):
        try:
            with engine.begin() as c:
                row = c.execute(text("""
                    with picked as (
                      select id from run_task
                      where suite_run_id = :sr and state = 'queued'
                      order by exclusive desc, id
                      for update skip locked
                      limit 1)
                    update run_task t set state='claimed', worker_id=:w, claimed_at=now(), token=:tok
                    from picked where t.id = picked.id
                    returning t.id, t.test_id, t.test_version, t.exclusive, t.token"""),
                    {"sr": suite_run_id, "w": worker_id, "tok": mint_token()}).first()
                if row is None:
                    return None
                return {"run_task_id": str(row.id), "test_id": str(row.test_id),
                        "test_version": row.test_version, "exclusive": row.exclusive, "token": row.token}
        except IntegrityError:
            continue  # token collision → re-mint
    raise RuntimeError("claim_next: token minting kept colliding")


def load_bundle(engine, *, test_id, version):
    with engine.connect() as c:
        row = c.execute(text("select bundle from test_version where test_id=:t and version=:v"),
                        {"t": test_id, "v": version}).one()
        return row.bundle


def start_run_result(engine, *, run_task_id, test_id, test_version, environment):
    with engine.begin() as c:
        c.execute(text("update run_task set state='running' where id=:t"), {"t": run_task_id})
        row = c.execute(text("""
            insert into run_result (run_task_id, test_id, test_version, environment, verdict, started_at)
            values (:rt,:t,:v,:e,'running',now()) returning id"""),
            {"rt": run_task_id, "t": test_id, "v": test_version, "e": environment}).one()
        return str(row.id)


def finish_run_result(engine, *, run_result_id, run_task_id, test_id, verdict, error_class=None,
                      trace_path=None, video_path=None, screenshot_path=None, network_log_path=None,
                      duration_ms=None):
    with engine.begin() as c:
        c.execute(text("update run_result set pinned=false where test_id=:t and id<>:r"),
                  {"t": test_id, "r": run_result_id})   # D12: only the latest run per test is pinned
        c.execute(text("""
            update run_result set verdict=:vd, error_class=:ec, trace_path=:tp, video_path=:vp,
              network_log_path=:np, screenshot_path=:sp, duration_ms=:d, finished_at=now(), pinned=true
            where id=:r"""),
            {"vd": verdict, "ec": error_class, "tp": trace_path, "vp": video_path, "sp": screenshot_path,
             "np": network_log_path, "d": duration_ms, "r": run_result_id})
        c.execute(text("update run_task set state='done' where id=:t"), {"t": run_task_id})


def start_child_run(engine, *, parent_run_id, role):
    """B3: a retry/heal/verify replay as a REAL run_result. Inherits run_task/test/version/environment
    from the parent red run (same task — the run_task lifecycle is untouched: no state change, no
    pinning). Real id → unique artifact dir (fixes the fixed-dir 'reauthor-verify' overwrite) and an
    addressable step_event trail for the money-shot diff."""
    with engine.begin() as c:
        row = c.execute(text("""
            insert into run_result (run_task_id, test_id, test_version, environment, role,
                                    verdict, started_at)
            select run_task_id, test_id, test_version, environment, :role, 'running', now()
            from run_result where id = :parent returning id"""),
            {"parent": parent_run_id, "role": role}).one()
        return str(row.id)


def finish_child_run(engine, *, run_result_id, verdict, error_class=None, trace_path=None,
                     video_path=None, screenshot_path=None, network_log_path=None, duration_ms=None):
    """B3: finalize a child run. No pin churn (D12 pinning tracks primary runs only) and no
    run_task state change — the parent's lifecycle owns both."""
    with engine.begin() as c:
        c.execute(text("""
            update run_result set verdict=:vd, error_class=:ec, trace_path=:tp, video_path=:vp,
              network_log_path=:np, screenshot_path=:sp, duration_ms=:d, finished_at=now()
            where id=:r"""),
            {"vd": verdict, "ec": error_class, "tp": trace_path, "vp": video_path,
             "sp": screenshot_path, "np": network_log_path, "d": duration_ms, "r": run_result_id})


def set_suite_run_state(engine, *, suite_run_id, state, verdict=None, park_reason=None,
                        auth_reason=None, needs_human=None):
    # auth_reason/needs_human (§3.5): coalesce so non-auth state transitions leave them untouched; the
    # probe gate's _park passes them on an auth park so the UI can show the verbatim reason + badge.
    with engine.begin() as c:
        c.execute(text("""update suite_run set state=:s, verdict=coalesce(:v, verdict),
                          park_reason=coalesce(:p, park_reason),
                          auth_reason=coalesce(:ar, auth_reason),
                          needs_human=coalesce(:nh, needs_human) where id=:sr"""),
                  {"s": state, "v": verdict, "p": park_reason, "ar": auth_reason,
                   "nh": needs_human, "sr": suite_run_id})


def cancel_queued_tasks(engine, *, suite_run_id):
    with engine.begin() as c:
        c.execute(text("update run_task set state='cancelled' where suite_run_id=:sr and state='queued'"),
                  {"sr": suite_run_id})


# ── reads (the console / API data source, D32) ──────────────────────────────
def list_runs(engine, *, project_id, limit=50, offset=0, test_id=None, verdict=None,
              environment=None, pinned=None, trigger=None, since=None, until=None, role="primary"):
    # A2: full row projection (off_canonical/param_row/browser/viewport/test_version/incident_id) +
    # trigger via the run_task → suite_run join — the Runs-list columns (runs-list.html).
    # A3: offset pagination + the Runs-list filters (all optional, parameterized).
    # B3: role defaults to 'primary' so run history stays clean; 'any' or retry|heal|verify widens it.
    filters = {"rr.test_id = :test_id": test_id, "rr.verdict = :verdict": verdict,
               "rr.environment = :environment": environment, "rr.pinned = :pinned": pinned,
               "sr.trigger = :trigger": trigger,
               "rr.started_at >= :since": since, "rr.started_at < :until": until,
               "rr.role = :role": None if role in (None, "any") else role}
    where = " and ".join(["t.project_id = :p"] + [f for f, v in filters.items() if v is not None])
    params = {"p": project_id, "l": limit, "o": offset,
              **{f.split(":")[1]: v for f, v in filters.items() if v is not None}}
    with engine.connect() as c:
        rows = c.execute(text(f"""
            select rr.id, rr.test_id, t.test_key, rr.test_version, rr.role, rr.verdict, rr.error_class,
                   rr.environment, rr.duration_ms, rr.pinned, rr.off_canonical, rr.param_row,
                   rr.browser, rr.viewport, rr.incident_id, sr.trigger, rr.started_at, rr.finished_at
            from run_result rr
            join test t on t.id = rr.test_id
            join run_task rt on rt.id = rr.run_task_id
            join suite_run sr on sr.id = rt.suite_run_id
            where {where}
            order by rr.started_at desc nulls last limit :l offset :o"""),
            params).mappings().all()
        return [dict(r) for r in rows]


# ── labels: the catalog's cross-cutting axis (a suite you can run without a membership array) ────
# `verified_against` rides along on the catalog read because it is the head version's CANONICAL TARGET
# KEY — what lets a label run resolve its own target instead of asking (see labels_targets below).
_TEST_COLS = """t.id, t.test_key, t.head_version, t.quarantined, t.labels,
                (select tv.verified_against from test_version tv
                  where tv.test_id = t.id and tv.version = t.head_version) as verified_against"""


def list_tests(engine, *, project_id, label=None):
    """A2: the test catalog (test-viewer entry, suite membership modal). `label` filters to tests
    carrying it — the empty string means "carrying none" (the Unlabelled bucket)."""
    where, params = "t.project_id=:p", {"p": project_id}
    if label == "":
        where += " and cardinality(t.labels) = 0"
    elif label is not None:
        where += " and :l = any(t.labels)"
        params["l"] = label
    with engine.connect() as c:
        rows = c.execute(text(f"select {_TEST_COLS} from test t where {where} order by t.test_key"),
                         params).mappings().all()
        return [dict(r) for r in rows]


def list_labels(engine, *, project_id):
    """Every label in use, with how many tests carry it. Derived by unnest — labels have no registry
    table, so a label exists exactly as long as some test carries it."""
    with engine.connect() as c:
        rows = c.execute(text("""
            select l as label, count(*) as test_count
            from test t, unnest(t.labels) as l
            where t.project_id = :p
            group by l order by l"""), {"p": project_id}).mappings().all()
        return [dict(r) for r in rows]


def set_test_labels(engine, *, test_id, labels) -> bool:
    """Replace a test's label set. The ONE write path — the API's PATCH, authoring, and the seeds all
    come through here. Returns False if the test is missing. Relabelling writes a changelog row (it is
    auditable) but never a test_version (it is not a behaviour change)."""
    with engine.begin() as c:
        row = c.execute(text("""
            update test set labels = :l where id = :t
            returning project_id, test_key, labels"""),
            {"t": test_id, "l": list(labels)}).mappings().first()
        if row is None:
            return False
        shown = ", ".join(row["labels"]) or "none"
        c.execute(text("""
            insert into changelog (project_id, test_id, kind, summary)
            values (:p, :t, 'labels', :s)"""),
            {"p": row["project_id"], "t": test_id, "s": f"labels set to [{shown}]"})
        return True


def tests_with_label(engine, *, project_id, label, runnable_only=True):
    """The members of a label run: id + canonical target key, head version only. Quarantined and
    never-frozen tests are excluded — a quarantined test is one the operator has taken out of the
    rotation, and silently running it would contradict that (the console still LISTS it, marked).

    `runnable_only=False` returns everything carrying the label, which is how the caller can tell
    "nothing carries this label" apart from "everything carrying it is sidelined"."""
    gate = "" if not runnable_only else "and not t.quarantined and t.head_version is not null"
    with engine.connect() as c:
        rows = c.execute(text(f"""
            select {_TEST_COLS} from test t
            where t.project_id = :p and :l = any(t.labels) {gate}
            order by t.test_key"""), {"p": project_id, "l": label}).mappings().all()
        return [dict(r) for r in rows]


def list_suites(engine, *, project_id):
    with engine.connect() as c:
        rows = c.execute(text("select id, project_id, name, member_test_ids "
                              "from suite where project_id=:p order by name"),
                         {"p": project_id}).mappings().all()
        return [dict(r) for r in rows]


def get_suite(engine, *, suite_id):
    with engine.connect() as c:
        r = c.execute(text("select id, project_id, name, member_test_ids from suite where id=:s"),
                      {"s": suite_id}).mappings().first()
        return dict(r) if r else None


def get_suite_run(engine, *, suite_run_id):
    """A2: post-hoc suite-run inspection — and the terminal-state poll target B1's CLI/console use."""
    with engine.connect() as c:
        r = c.execute(text("select id, project_id, suite_id, mode, trigger, target_id, state, "
                           "verdict, park_reason, auth_reason, needs_human, created_at "
                           "from suite_run where id=:sr"),
                      {"sr": suite_run_id}).mappings().first()
        return dict(r) if r else None


def suite_run_results(engine, *, suite_run_id):
    """The run_results belonging to one suite_run (via its run_tasks)."""
    with engine.connect() as c:
        rows = c.execute(text("""
            select rr.id, rr.test_id, t.test_key, rr.verdict, rr.error_class, rr.duration_ms,
                   rr.started_at, rr.finished_at
            from run_result rr
            join run_task rt on rt.id = rr.run_task_id
            join test t on t.id = rr.test_id
            where rt.suite_run_id = :sr order by rr.started_at nulls last"""),
            {"sr": suite_run_id}).mappings().all()
        return [dict(r) for r in rows]


def get_territory_notes(engine, *, target_id):
    """A2: the read path for D25 TerritoryNotes (upsert_territory_notes was write-only). Latest version."""
    with engine.connect() as c:
        r = c.execute(text("""select target_id, version, notes, stale, authored_by, created_at
                              from territory_notes where target_id=:t
                              order by version desc limit 1"""),
                      {"t": target_id}).mappings().first()
        return dict(r) if r else None


def get_golden(engine, *, test_id):
    """B4: the per-test golden reference pointer + provenance (test-viewer golden panel), or None."""
    with engine.connect() as c:
        r = c.execute(text("select test_id, video_path, source, reflects_version, created_at "
                           "from test_golden where test_id=:t"), {"t": test_id}).mappings().first()
        return dict(r) if r else None


def set_golden(engine, *, test_id, video_path, source, reflects_version=None):
    """B4: upsert the golden (one per test — replace overwrites). source ∈ auto|manual."""
    with engine.begin() as c:
        c.execute(text("""
            insert into test_golden (test_id, video_path, source, reflects_version)
            values (:t,:v,:s,:rv)
            on conflict (test_id) do update set video_path=excluded.video_path,
              source=excluded.source, reflects_version=excluded.reflects_version,
              created_at=now()"""),
            {"t": test_id, "v": video_path, "s": source, "rv": reflects_version})


def clear_golden(engine, *, test_id) -> bool:
    with engine.begin() as c:
        return c.execute(text("delete from test_golden where test_id=:t"),
                         {"t": test_id}).rowcount > 0


def list_sessions(engine, *, project_id):
    """A2: session health per persona (state/last_validated/seeded_at) — the parked_auth recovery
    surface (sessions.html). The storage-state path stays server-side (it holds live cookies)."""
    with engine.connect() as c:
        rows = c.execute(text("""
            select c2.id as credential_id, c2.persona, s.state, s.seeded_at, s.last_validated
            from credential c2 left join session s on s.credential_id = c2.id
            where c2.project_id = :p order by c2.persona"""),
            {"p": project_id}).mappings().all()
        return [{**dict(r), "state": r["state"] or "unknown"} for r in rows]


def list_targets(engine, *, project_id):
    with engine.connect() as c:
        rows = c.execute(text("select id, project_id, key, base_url, timezone, date_format, "
                              "max_parallel, profile from target where project_id=:p order by key"),
                         {"p": project_id}).mappings().all()
        return [dict(r) for r in rows]


def get_run(engine, *, run_result_id):
    with engine.connect() as c:
        r = c.execute(text("""
            select rr.*, t.test_key, tv.bundle->'intent'->>'descriptor' as intent_descriptor,
                   tv.bundle->'intent'->'sources'->0->>'verbatim' as intent_nl
            from run_result rr
            join test t on t.id = rr.test_id
            join test_version tv on tv.test_id = rr.test_id and tv.version = rr.test_version
            where rr.id = :r"""), {"r": run_result_id}).mappings().first()
        return dict(r) if r else None


def get_steps(engine, *, run_result_id, after=0):
    # B1: `id` (bigserial, monotonic) is the poll/SSE cursor — ?after={id} returns only new rows.
    # Ordered by id (== step order today; stays correct when B2 adds step-started rows).
    with engine.connect() as c:
        rows = c.execute(text("""
            select id, step_index, step_id, intent, action, rung_used, status, error_class,
                   budget_ms, elapsed_ms, video_offset_ms, network, detail
            from step_event where run_result_id = :r and id > :after order by id"""),
            {"r": run_result_id, "after": after or 0}).mappings().all()
        return [dict(r) for r in rows]


def suite_run_step_events_after(engine, *, suite_run_id, after=0):
    """B1: the suite-level SSE tail — new step_events across ALL of a suite_run's member runs,
    tagged with run_result_id + test_id so the client can multiplex per-test progress."""
    with engine.connect() as c:
        rows = c.execute(text("""
            select se.id, se.run_result_id, rr.test_id, t.test_key, se.step_index, se.step_id,
                   se.intent, se.action, se.rung_used, se.status, se.error_class, se.elapsed_ms,
                   se.video_offset_ms
            from step_event se
            join run_result rr on rr.id = se.run_result_id
            join run_task rt on rt.id = rr.run_task_id
            join test t on t.id = rr.test_id
            where rt.suite_run_id = :sr and se.id > :after order by se.id"""),
            {"sr": suite_run_id, "after": after or 0}).mappings().all()
        return [dict(r) for r in rows]


def active_suite_run(engine, *, target_id):
    """B1: the currently-active suite_run for a target (the launch lock holder), or None."""
    with engine.connect() as c:
        r = c.execute(text("select id from suite_run where target_id=:t "
                           "and state in ('pending','probing','running') limit 1"),
                      {"t": target_id}).first()
        return str(r.id) if r else None


def reap_interrupted(engine) -> int:
    """B1 startup reaper: an API restart mid-run leaves suite_runs stuck 'running' forever (the run
    thread died with the process). Terminal-ize them with the DISTINCT park_reason INTERRUPTED —
    deliberately NOT ENV_INFRA (the env wasn't down; the server bounced) so park-rate analytics stay
    honest. Also releases the one_active_suite_run_per_target launch lock. Returns suite_runs reaped."""
    with engine.begin() as c:
        c.execute(text("""
            update run_result set verdict='interrupted', error_class='INTERRUPTED', finished_at=now()
            where verdict='running' and run_task_id in (
              select rt.id from run_task rt join suite_run sr on sr.id = rt.suite_run_id
              where sr.state in ('pending','probing','running'))"""))
        return c.execute(text("""
            update suite_run set state='parked', verdict='interrupted', park_reason='INTERRUPTED'
            where state in ('pending','probing','running')""")).rowcount


def count_step_events(engine, *, run_result_id) -> int:
    """The number of persisted TERMINAL step_events for a run — the D33 evidence-trail completeness
    check. Excludes B2's live 'running' rows (two rows per step) or a pass could finalize over an
    incomplete trail (started-but-never-completed steps would inflate the count)."""
    with engine.connect() as c:
        return c.execute(text("select count(*) from step_event "
                              "where run_result_id = :r and status <> 'running'"),
                         {"r": run_result_id}).scalar() or 0


# ── M3 red path: versions, incidents (§8/§9), budgets (D27), changelog (D37) ─────────────────────
def create_test_version(engine, *, test_id, bundle: dict, reason, parent_version, verified_against,
                        state="frozen", incident_id=None, triggered_by=None, ruling=None,
                        calibration=None, apply=True) -> int:
    """Insert vN+1 (heal auto-apply D3, or a provisional re-author §9). `apply` bumps head_version so
    the new version is live immediately (provisional counts as applied — §6 'never block the suite')."""
    with engine.begin() as c:
        nxt = c.execute(text("select coalesce(max(version),0)+1 v from test_version where test_id=:t"),
                        {"t": test_id}).one().v
        c.execute(text("""
            insert into test_version (test_id, version, bundle, state, reason, parent_version,
                                      incident_id, triggered_by, verified_against, ruling, calibration)
            values (:t,:v, cast(:b as jsonb), :st, :r, :pv, :iid, :tb, :va, :rl, cast(:cal as jsonb))"""),
            {"t": test_id, "v": nxt, "b": json.dumps(bundle), "st": state, "r": reason, "pv": parent_version,
             "iid": incident_id, "tb": triggered_by, "va": verified_against, "rl": ruling,
             "cal": json.dumps(calibration) if calibration is not None else None})
        if apply:
            c.execute(text("update test set head_version=:v where id=:t"), {"v": nxt, "t": test_id})
        return nxt


def get_test_version(engine, *, test_id, version):
    # A2: calibration (D7 — "when was this oracle last proven falsifiable") + provenance included.
    with engine.connect() as c:
        r = c.execute(text("select test_id, version, bundle, state, reason, parent_version, incident_id, "
                           "ruling, triggered_by, verified_against, calibration, created_at "
                           "from test_version where test_id=:t and version=:v"),
                      {"t": test_id, "v": version}).mappings().first()
        return dict(r) if r else None


def list_test_versions(engine, *, test_id):
    with engine.connect() as c:
        rows = c.execute(text("select version, state, reason, parent_version, incident_id, ruling, "
                              "triggered_by, created_at "
                              "from test_version where test_id=:t order by version"), {"t": test_id}).mappings().all()
        return [dict(r) for r in rows]


def set_version_state(engine, *, test_id, version, state, ruling=None):
    with engine.begin() as c:
        c.execute(text("update test_version set state=:s, ruling=coalesce(:r, ruling) "
                       "where test_id=:t and version=:v"),
                  {"s": state, "r": ruling, "t": test_id, "v": version})


def set_head_version(engine, *, test_id, version):
    with engine.begin() as c:
        c.execute(text("update test set head_version=:v where id=:t"), {"v": version, "t": test_id})


def set_quarantined(engine, *, test_id, quarantined=True):
    with engine.begin() as c:
        c.execute(text("update test set quarantined=:q where id=:t"), {"q": quarantined, "t": test_id})


def get_test(engine, *, test_id):
    with engine.connect() as c:
        r = c.execute(text(f"select {_TEST_COLS}, t.project_id from test t where t.id=:t"),
                      {"t": test_id}).mappings().first()
        return dict(r) if r else None


def attach_incident(engine, *, project_id, klass, surface, step_sig, error_class,
                    cap_agent_runs, cap_llm_calls, test_id, red_run_id) -> tuple[str, bool]:
    """3.1a — upsert to the open-incident partial unique index (§9 step 1). N parallel reds with the
    same cluster key converge to ONE open incident; the loser's insert collides and returns the same
    id. Adds the red test as a member, links the red run, and sets the representative if unset. Returns
    (incident_id, created)."""
    with engine.begin() as c:
        row = c.execute(text("""
            insert into incident (project_id, klass, ck_surface, ck_step_sig, ck_error_class,
                                  cap_agent_runs, cap_llm_calls)
            values (:p,:k,:s,:sig,:ec,:ca,:cl)
            on conflict (project_id, ck_surface, ck_step_sig, ck_error_class) where state in ('open','propagating')
              do update set state = incident.state
            returning id, (xmax = 0) as created"""),
            {"p": project_id, "k": klass, "s": surface, "sig": step_sig, "ec": error_class,
             "ca": cap_agent_runs, "cl": cap_llm_calls}).one()
        iid, created = str(row.id), row.created
        c.execute(text("""insert into incident_member (incident_id, test_id, red_run_id)
                          values (:i,:t,:r) on conflict (incident_id, test_id) do nothing"""),
                  {"i": iid, "t": test_id, "r": red_run_id})
        c.execute(text("update incident set representative_test_id=:t "
                       "where id=:i and representative_test_id is null"), {"t": test_id, "i": iid})
        if red_run_id:
            c.execute(text("update run_result set incident_id=:i where id=:r"), {"i": iid, "r": red_run_id})
        return iid, created


def spend_agent_run(engine, *, incident_id) -> bool:
    """3.1b — atomic budget guard (D27). The WHERE is the guard; no read-then-write window. Returns
    True if a run was granted, False if the per-incident cap is exhausted (→ partial results at inbox)."""
    with engine.begin() as c:
        row = c.execute(text("""update incident set budget_agent_runs = budget_agent_runs + 1
            where id=:i and budget_agent_runs < cap_agent_runs returning budget_agent_runs"""),
            {"i": incident_id}).first()
        return row is not None


def set_incident(engine, *, incident_id, **fields):
    """Update mutable incident columns (state, change_brief, ruling, ruling_note, ruled_by, ruled_at,
    representative_test_id, closed_at)."""
    cols = {"state", "change_brief", "ruling", "ruling_note", "ruled_by", "representative_test_id"}
    sets, params = [], {"i": incident_id}
    for k, v in fields.items():
        if k in cols:
            sets.append(f"{k}=:{k}"); params[k] = v
    if fields.get("ruled"):
        sets.append("ruled_at=now()")
    if fields.get("closed"):
        sets.append("closed_at=now()")
    if not sets:
        return
    with engine.begin() as c:
        c.execute(text(f"update incident set {', '.join(sets)} where id=:i"), params)


def set_rep_concluded(engine, *, incident_id):
    """Mark the incident's discoverer as CONCLUDED (any outcome) — the event that unblocks guided members
    waiting on the change_brief (§9.4). Set on every discoverer exit path, including a crash (a finally)."""
    with engine.begin() as c:
        c.execute(text("update incident set rep_concluded=true where id=:i"), {"i": incident_id})


def set_member_applied(engine, *, incident_id, test_id, applied_version):
    with engine.begin() as c:
        c.execute(text("update incident_member set applied_version=:v where incident_id=:i and test_id=:t"),
                  {"v": applied_version, "i": incident_id, "t": test_id})


def set_member_ruling(engine, *, incident_id, test_id, ruling):
    """A5: the per-member decision (feature|bug) — money-shot Direction B rules each member."""
    with engine.begin() as c:
        c.execute(text("update incident_member set ruling=:r where incident_id=:i and test_id=:t"),
                  {"r": ruling, "i": incident_id, "t": test_id})


def get_incident(engine, *, incident_id):
    with engine.connect() as c:
        r = c.execute(text("select * from incident where id=:i"), {"i": incident_id}).mappings().first()
        return dict(r) if r else None


def list_incidents(engine, *, project_id, state=None, klass=None, ruling=None,
                   since=None, until=None, limit=50, offset=0):
    # A3: pagination + klass/ruling/date filters (was state-only).
    filters = {"state = :state": state, "klass = :klass": klass, "ruling = :ruling": ruling,
               "created_at >= :since": since, "created_at < :until": until}
    where = " and ".join(["project_id = :p"] + [f for f, v in filters.items() if v is not None])
    params = {"p": project_id, "l": limit, "o": offset,
              **{f.split(":")[1]: v for f, v in filters.items() if v is not None}}
    with engine.connect() as c:
        rows = c.execute(text(f"select * from incident where {where} "
                              "order by created_at desc limit :l offset :o"), params).mappings().all()
        return [dict(r) for r in rows]


def incident_members(engine, *, incident_id):
    with engine.connect() as c:
        rows = c.execute(text("""select im.test_id, im.red_run_id, im.verify_run_id,
                                 im.applied_version, im.ruling,
                                 t.test_key, t.head_version, t.quarantined
                                 from incident_member im join test t on t.id = im.test_id
                                 where im.incident_id=:i order by t.test_key"""),
                         {"i": incident_id}).mappings().all()
        return [dict(r) for r in rows]


def set_member_verify_run(engine, *, incident_id, test_id, verify_run_id):
    """B3: link the verify replay that PROVED this member's provisional (money-shot evidence)."""
    with engine.begin() as c:
        c.execute(text("update incident_member set verify_run_id=:v "
                       "where incident_id=:i and test_id=:t"),
                  {"v": verify_run_id, "i": incident_id, "t": test_id})


def count_test_cluster_incidents(engine, *, test_id, surface, step_sig, error_class):
    """How many incidents this test has ALREADY been a member of for this exact cluster key (any state).
    The oscillation signal (D27): the SAME (surface, step_signature, error_class) recurring for a test —
    keyed on the cluster, so distinct drifts on different steps don't falsely accumulate, and only
    genuine incidents count (a converged green/provisional run opens no new matching incident)."""
    with engine.connect() as c:
        r = c.execute(text("""select count(*) n from incident i
                              join incident_member im on im.incident_id = i.id
                              where im.test_id=:t and i.ck_surface=:s and i.ck_step_sig=:sig
                                and i.ck_error_class=:ec"""),
                      {"t": test_id, "s": surface, "sig": step_sig, "ec": error_class}).one()
        return r.n


def changelog_add(engine, *, project_id, kind, summary, test_id=None, incident_id=None, ruling_note=None):
    with engine.begin() as c:
        c.execute(text("""insert into changelog (project_id, test_id, incident_id, kind, summary, ruling_note)
                          values (:p,:t,:i,:k,:s,:n)"""),
                  {"p": project_id, "t": test_id, "i": incident_id, "k": kind, "s": summary, "n": ruling_note})


def list_changelog(engine, *, project_id, incident_id=None, test_id=None, kind=None,
                   since=None, limit=100, offset=0):
    # A3: test_id/kind/since filters + pagination (changelog.html, test-viewer › Changelog).
    filters = {"incident_id = :incident_id": incident_id, "test_id = :test_id": test_id,
               "kind = :kind": kind, "created_at >= :since": since}
    where = " and ".join(["project_id = :p"] + [f for f, v in filters.items() if v is not None])
    params = {"p": project_id, "l": limit, "o": offset,
              **{f.split(":")[1]: v for f, v in filters.items() if v is not None}}
    with engine.connect() as c:
        rows = c.execute(text(f"select * from changelog where {where} "
                              "order by created_at desc limit :l offset :o"), params).mappings().all()
        return [dict(r) for r in rows]


def fallback_steps(engine, *, run_result_id):
    """The steps in a run that PASSED on a fallback rung (rung_used ≠ primary) — each auto-opens a
    re-freeze incident (D28, §6.1). Returns the passed+fallback step_events."""
    with engine.connect() as c:
        # the schema's `fallback` flag lives in the detail JSONB (sink.py, §3 has no fallback column)
        rows = c.execute(text("""select step_index, step_id, intent, action, rung_used, detail
                                 from step_event where run_result_id=:r and status='passed'
                                   and (detail->>'fallback')::boolean = true
                                 order by step_index"""), {"r": run_result_id}).mappings().all()
        return [dict(r) for r in rows]


# ── M4 scale: sweeper (§10.2) + change-driven selection (§11) ────────────────────────────────────
def suite_run_tokens(engine, *, suite_run_id):
    """Distinct run tokens minted for this suite run (the sweeper's D15 allowlist)."""
    with engine.connect() as c:
        rows = c.execute(text("select distinct token from run_task where suite_run_id=:s and token is not null"),
                         {"s": suite_run_id}).all()
        return [r[0] for r in rows]


def suite_run_network(engine, *, suite_run_id):
    """Flattened network entries across all step_events of this suite run's runs (sweeper endpoint
    derivation; the surface inventory re-observes from every run's network log, §11)."""
    with engine.connect() as c:
        rows = c.execute(text("""select se.network from step_event se
                                 join run_result rr on rr.id = se.run_result_id
                                 join run_task rt on rt.id = rr.run_task_id
                                 where rt.suite_run_id = :s and se.network is not null"""),
                         {"s": suite_run_id}).all()
    out = []
    for (net,) in rows:
        out.extend(net or [])
    return out


def project_tests_with_head_bundle(engine, *, project_id):
    """Each non-quarantined test with its head bundle (selection §11 needs coverage descriptor + the
    surfaces the test touches). Returns [{test_id, test_key, bundle}]."""
    with engine.connect() as c:
        rows = c.execute(text("""select t.id, t.test_key, tv.bundle
                                 from test t join test_version tv
                                   on tv.test_id = t.id and tv.version = t.head_version
                                 where t.project_id = :p and t.quarantined = false"""),
                         {"p": project_id}).mappings().all()
        return [{"test_id": str(r["id"]), "test_key": r["test_key"], "bundle": r["bundle"]} for r in rows]


# ── retention (D12) ──────────────────────────────────────────────────────────
def prune_runs(engine, *, keep_pinned=True, older_than_days=7):
    """D12 retention: prune run history older than `older_than_days` (default 7). Exemptions: the pinned
    latest run per test; in-flight runs (verdict='running'); and any run referenced by a NON-CLOSED
    incident (state <> 'closed') — a live/ruled-but-open incident's triggering run is the evidence the
    inbox still shows, so it survives regardless of age (like pinned). A non-pinned run younger than the
    window also survives.

    For a prunable run referenced ONLY by CLOSED incidents, its incident_member.red_run_id is NULLed
    first, so the D12 metadata (which test was a member) outlives the aged-out artifact while the strict
    fk_member_run FK is preserved — a wrongful delete of a still-referenced run still fails LOUD. Deletes
    step_events first (FK has no ON DELETE CASCADE). Returns the number of run_results deleted."""
    clauses = ["verdict <> 'running'", "finished_at is not null",
               f"finished_at < now() - interval '{int(older_than_days)} days'",
               # a run tied to a still-open incident (ANY state but 'closed') is live evidence — never prune.
               # B3: the verify replay is the incident's OTHER half of the evidence — same rule.
               "id not in (select red_run_id from incident_member m join incident i on i.id = m.incident_id "
               "where i.state <> 'closed' and m.red_run_id is not null)",
               "id not in (select verify_run_id from incident_member m join incident i on i.id = m.incident_id "
               "where i.state <> 'closed' and m.verify_run_id is not null)"]
    if keep_pinned:
        clauses.append("pinned = false")
    where = "where " + " and ".join(clauses)
    with engine.begin() as c:
        # closed-incident refs to the aging-out runs → NULL (metadata survives; the artifact prunes, D12).
        # Runs tied to a non-closed incident are excluded from {where}, so only closed-incident refs match.
        c.execute(text(f"""update incident_member set red_run_id = null where red_run_id in
                           (select id from run_result {where})"""))
        c.execute(text(f"""update incident_member set verify_run_id = null where verify_run_id in
                           (select id from run_result {where})"""))
        c.execute(text(f"""delete from step_event where run_result_id in
                           (select id from run_result {where})"""))
        deleted = c.execute(text(f"delete from run_result {where}")).rowcount
        return deleted
