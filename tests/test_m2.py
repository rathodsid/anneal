"""M2 (marker: m2) — the HOT lane. Proves each HLD §15 exit criterion:

- a CRUD flow authored from NL ALONE freezes, CALIBRATES (negative-control + network
  discriminativeness), and replays green TWICE with distinct tokens and ZERO residue;
- the ambiguity interview BLOCKS on unanswered questions (never guessed);
- the destructive guard HOLDS in the authored flow;
- recon persists TerritoryNotes (D25).

Authoring uses the live LLM (keys from the gitignored .env) against the hermetic golden app.
The bundle is authored ONCE (module fixture) and reused — freezing is the determinism boundary.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request

import pytest
from sqlalchemy import text

from anneal_artifacts.store import LocalDiskStore
from anneal_authoring.interview import open_questions
from anneal_authoring.service import author_and_freeze
from anneal_core.bundle import Bundle
from anneal_db import repositories as repo
from anneal_db.engine import get_engine

from conftest import GOLDEN_URL

# FK-safe order (children before parents; incident_member.red_run_id→run_result, added in M3)
_TABLES = ["step_event", "incident_member", "run_result", "run_task", "test_version", "test_golden", "suite_run",
           "suite", "changelog", "incident", "test", "module_version", "module", "session",
           "credential", "territory_notes", "target", "project"]

CRUD_NL = ("Create a new campaign with any name. Then open the campaigns list and verify the new "
           "campaign appears in the list. Then open that campaign's row menu, choose Delete, confirm "
           "the deletion, and verify the campaign is gone from the list.")
AMBIGUOUS_NL = ("Create a campaign and assign it one of the available agents from the agent dropdown — "
                "you must decide which specific agent to use — then submit and verify that agent is "
                "shown on the campaign.")


def _golden_campaign_names() -> list[str]:
    data = json.loads(urllib.request.urlopen(f"{GOLDEN_URL}/api/v2/campaigns").read().decode())
    return [c.get("name", "") for c in data.values()]


def _cleanup_authored_residue():
    data = json.loads(urllib.request.urlopen(f"{GOLDEN_URL}/api/v2/campaigns").read().decode())
    for cid, c in list(data.items()):
        if c.get("name", "").startswith("Item "):
            req = urllib.request.Request(f"{GOLDEN_URL}/api/v2/campaigns/{cid}", method="DELETE")
            urllib.request.urlopen(req).read()


@pytest.fixture(scope="module")
def m2_env():
    eng = get_engine()
    with eng.begin() as c:
        for t in _TABLES:
            c.execute(text(f"delete from {t}"))
    tid = repo.seed_project_target(eng, project_id="golden", project_name="Golden",
                                   target_key="golden-local", base_url=GOLDEN_URL, timezone_="UTC",
                                   profile={"probe_path": "/campaigns"})
    _cleanup_authored_residue()
    return {"engine": eng, "target_id": tid}


@pytest.fixture(scope="module")
def frozen(m2_env):
    """Author the CRUD flow from NL once, freeze it, and expose the frozen test + a suite."""
    eng, tid = m2_env["engine"], m2_env["target_id"]
    res = asyncio.run(author_and_freeze(eng, project_id="golden", test_key="authored-crud",
                                        nl=CRUD_NL, target_id=tid))
    assert res["status"] == "frozen", f"authoring did not freeze: {res}"
    suite = repo.create_suite(eng, project_id="golden", name="m2-crud",
                              member_test_ids=[res["test_id"]])
    _cleanup_authored_residue()
    return {**m2_env, "test_id": res["test_id"], "bundle": res["bundle"],
            "calibration": res["calibration"], "suite": suite}


@pytest.mark.m2
def test_authored_bundle_is_frozen_and_calibrated(frozen):
    b = frozen["bundle"]
    assert b["state"] == "frozen" and b["policy"]["mutating"] is True
    # volatile-ized: the created item's name is {{run.token}}, not a frozen literal (B1/D5)
    assert "{{run.token}}" in json.dumps(b["steps"])
    # a destructive step was authored + flagged (D15)
    assert any(s.get("destructive") for s in b["steps"]), "no destructive step authored"
    # calibration proves the oracle is falsifiable (D7)
    calib = frozen["calibration"]
    assert any(v.get("negative_control") == "failed_on_pre_state" for v in calib.values()), calib
    assert any(v.get("network_discriminativeness") == "unique_in_authoring_log" for v in calib.values()), calib
    # (laundered-goto regression) the flow must ground its interactive acts as REAL verbs, never degrade
    # an ungroundable act into a no-op `goto` back to the start path — the exact bug the author-lane fix
    # closes. A CRUD flow that froze with no click/fill/select is a filler-goto bundle masquerading green.
    verbs = {s["action"] for s in b["steps"]}
    assert verbs & {"click", "fill", "select", "keypress"}, f"flow froze with no interactive acts: {verbs}"
    start = b["steps"][0].get("arg")
    fillers = [s for s in b["steps"][1:] if s["action"] == "goto" and s.get("arg") in (None, "", start)]
    assert not fillers, f"interactive steps degraded to goto-start: {[s['intent'] for s in fillers]}"
    # schema-valid
    Bundle.model_validate(b)


@pytest.mark.m2
async def test_replays_green_twice_distinct_tokens_zero_residue(frozen, tmp_path):
    from anneal_scheduler.scheduler import run_suite
    eng, tid, suite = frozen["engine"], frozen["target_id"], frozen["suite"]
    art = LocalDiskStore(str(tmp_path / "m2art"))
    tokens = []
    for _ in range(2):
        sr = repo.create_suite_run(eng, project_id="golden", suite_id=suite, target_id=tid)
        report = await run_suite(eng, art, suite_run_id=sr, target_id=tid, max_parallel=1)
        assert report.verdict == "green", f"authored flow did not replay green: {report}"
        with eng.connect() as c:
            tok = c.execute(text("select token from run_task where suite_run_id=:s"), {"s": sr}).scalar()
        tokens.append(tok)
    assert tokens[0] != tokens[1], "replays did not use distinct run tokens"
    residue = [n for n in _golden_campaign_names() if n.startswith("Item ")]
    assert residue == [], f"authored flow left residue after two runs: {residue}"


@pytest.mark.m2
def test_interview_blocks_then_proceeds_when_answered(m2_env):
    eng, tid = m2_env["engine"], m2_env["target_id"]
    # unanswered ambiguity → BLOCKED with questions (never guessed)
    blocked = asyncio.run(author_and_freeze(eng, project_id="golden", test_key="ambiguous-x",
                                            nl=AMBIGUOUS_NL, target_id=tid))
    assert blocked["status"] == "needs_answers", blocked
    assert blocked["questions"], "interview raised no questions for an ambiguous flow"
    # answering the open question(s) unblocks the interview (freeze proceeds)
    answers = {q: "Acme" for q in blocked["questions"]}
    remaining = open_questions(AMBIGUOUS_NL, answers)
    assert remaining == [], f"interview still blocked after answering: {remaining}"
    _cleanup_authored_residue()


@pytest.mark.m2
async def test_destructive_guard_holds_in_authored_flow(frozen, tmp_path):
    """Replay the authored bundle with a guard token that does NOT match the created item's identity:
    the destructive delete must be REFUSED (POLICY_STOP), not performed off-token (D15/§6.4)."""
    from anneal_core.compile import compile as compile_bundle
    from anneal_core.stepplan import CompileContext
    from anneal_executor.evidence import recording
    from anneal_executor.interpreter import Interpreter
    from datetime import date

    art = LocalDiskStore(str(tmp_path / "guardart"))

    class Sink:
        def __init__(s): s.events = []
        def emit(s, rid, ev): s.events.append(ev)

    # compile the item name with token A, but hand the interpreter's guard a DIFFERENT token B
    plan = compile_bundle(Bundle.model_validate(frozen["bundle"]),
                          CompileContext(token="gooda1b2c3", today=date.today(),
                                         date_format="%Y-%m-%d", params={}, modules={}))
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch()
        async with recording(b, art, "guard", base_url=GOLDEN_URL) as (page, ev):
            outcome = await Interpreter.cold(page, plan, Sink(), art,
                                             token="badx9y8z7w", run_result_id="guard").run()
        await b.close()
    assert outcome.verdict == "red" and outcome.error_class == "POLICY_STOP", \
        f"destructive guard did not hold: {outcome}"
    _cleanup_authored_residue()          # the create ran before the guarded delete → clean it up


@pytest.mark.m2
async def test_authored_delete_flags_token_bearing_confirm_replays_green(frozen, tmp_path):
    """Regression guard for the destructive-flagging generalization gap the held-out set surfaced: the
    authored delete flow must flag the TOKEN-BEARING control (the confirm dialog that names the item),
    NOT a menu-open whose enclosing entity carries no token. On-token replay then passes the D15 guard
    (green) — a mis-flagged menu-open would `POLICY_STOP` → red here (the exact silent-regression case)."""
    from datetime import date

    from anneal_core.compile import compile as compile_bundle
    from anneal_core.stepplan import CompileContext
    from anneal_executor.evidence import recording
    from anneal_executor.interpreter import Interpreter

    b = frozen["bundle"]
    dest = [s for s in b["steps"] if s.get("destructive")]
    assert len(dest) == 1, f"expected exactly one destructive step, got {len(dest)}: {dest}"
    # the flag must sit on the control that actually PERFORMED the irreversible delete — the confirm that
    # fired a DELETE — never a menu-open precursor (no network) NOR the create-submit (a POST). Asserting
    # method==DELETE pins the exact regression: a mis-bind to the create POST or a token-bearing precursor
    # would leave the real delete unguarded and this fails.
    nets = [c for c in ((dest[0].get("postcondition") or {}).get("all") or []) if c.get("kind") == "network"]
    assert any((c.get("method") or "").upper() == "DELETE" for c in nets), \
        f"destructive flag is not on the DELETE-firing confirm: {dest[0]}"
    tok = "tokz9y8x7w"
    plan = compile_bundle(Bundle.model_validate(b),
                          CompileContext(token=tok, today=date.today(), date_format="%Y-%m-%d",
                                         params={}, modules={}))
    art = LocalDiskStore(str(tmp_path / "delart"))

    class Sink:
        def __init__(s): s.events = []
        def emit(s, rid, ev): s.events.append(ev)

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        br = await p.chromium.launch()
        async with recording(br, art, "del", base_url=GOLDEN_URL) as (page, ev):
            outcome = await Interpreter.cold(page, plan, Sink(), art, token=tok, run_result_id="del").run()
        await br.close()
    assert outcome.error_class != "POLICY_STOP", \
        f"destructive flag landed on a token-less control (menu-open, not the confirm): {outcome}"
    assert outcome.verdict in ("green", "green_with_heals"), f"authored delete flow did not replay green: {outcome}"
    _cleanup_authored_residue()


@pytest.mark.m2
def test_calibration_rejects_unfalsifiable_bundle(frozen):
    """D7 floor: a bundle with no negative-controlled present-assert and no discriminative network
    expectation has NOTHING to falsify — a green replay would be meaningless — so calibration must
    REJECT it, not freeze it. This proves the honesty guarantee via the rejection path, not merely
    that the CRUD flow happens to be falsifiable."""
    import copy

    from anneal_authoring.calibrate import CalibrationError, calibrate
    from playwright.async_api import async_playwright

    b = copy.deepcopy(frozen["bundle"])
    b["state"] = "draft"
    b.pop("calibration", None)
    for s in b["steps"]:                                  # strip every falsifiable signal
        pc = s.get("postcondition")
        if pc and pc.get("all"):
            pc["all"] = [c for c in pc["all"] if c.get("kind") != "network"]
            if not pc["all"]:
                s["postcondition"] = None
        if s.get("action") == "assert" and (s.get("assert") or {}).get("state") == "visible":
            s["assert"]["state"] = "hidden"               # a 'gone' assert is trivially true → vacuous

    async def _run():
        async with async_playwright() as p:
            br = await p.chromium.launch()
            try:
                await calibrate(br, GOLDEN_URL, b, "/campaigns")
            finally:
                await br.close()

    with pytest.raises(CalibrationError):
        asyncio.run(_run())


@pytest.mark.m2
def test_row_scope_carries_container_role():
    """A row scope resolves by the OBSERVED container's role: a control inside an <li> (sidebar, menu,
    card list) is scoped within role=listitem, not the fixed 'row' a table row gets — get_by_role('row')
    matches nothing on a list and the step reds LOCATOR_DRIFT at replay. Deterministic, no LLM."""
    from anneal_authoring.author import _target_from_descriptor
    li = _target_from_descriptor({"tag": "a", "href": "/x", "text": "Projects",
                                  "row": {"container_tag": "li", "label": "Projects"}})
    assert li["within"] == {"row_label": "Projects", "role": "listitem"}
    tr = _target_from_descriptor({"tag": "button", "name": "Row menu",
                                  "row": {"container_tag": "tr", "label": "Alpha"}})
    assert tr["within"] == {"row_label": "Alpha"}            # a table row keeps the default 'row'


@pytest.mark.m2
def test_landmark_scope_disambiguates_duplicate_rendering():
    """The same entity rendered in two regions (a nav entry AND a content card) is ambiguous page-wide
    but unique within its enclosing landmark: perception reports the landmark, the author re-scopes the
    ambiguous target to it, and the cold resolver then finds exactly one. Deterministic — no LLM."""
    from anneal_authoring.author import _landmark_scoped, _scope_ns, _target_from_descriptor, _target_ns
    from anneal_authoring.perception import snapshot_interactive
    from anneal_core.rungs import derive_rungs
    from anneal_executor.resolve import resolve
    from playwright.async_api import async_playwright

    async def _run():
        async with async_playwright() as p:
            br = await p.chromium.launch()
            try:
                page = await (await br.new_context()).new_page()
                await page.set_content(
                    '<nav><ul><li><a href="/p/1">Inbox</a></li></ul></nav>'
                    '<main><ul><li><a href="/p/1">Inbox</a> <span>3 tasks</span></li></ul></main>')
                els = await snapshot_interactive(page)
                nav = next(e for e in els if e.get("text") == "Inbox" and e["landmark"] == "navigation")
                assert any(e.get("text") == "Inbox" and e["landmark"] == "main" for e in els)
                tgt = _target_from_descriptor(nav)                 # within listitem 'Inbox' → 2 page-wide
                assert await page.get_by_role("listitem").filter(has_text="Inbox").count() == 2
                alt = _landmark_scoped(tgt, nav)
                assert alt["within"] == {"role": "navigation", "row_label": "Inbox"}
                loc, _ = await resolve(page, derive_rungs(_target_ns(alt)), 2000, _scope_ns(alt))
                assert await loc.count() == 1 and await loc.evaluate("e => e.closest('nav') !== null")
            finally:
                await br.close()
    import asyncio
    asyncio.run(_run())


@pytest.mark.m2
def test_assert_picker_sees_static_text_anchors():
    """On a page with nothing to click (a nav label and a heading) the interactive inventory is empty
    and the oracle picker would INVENT text from the step wording. Headings / testid text leaves are
    observed text: perception lists them, the regrounder keys on them (testid-first). No LLM."""
    from anneal_authoring.author import _exact_descriptor, _target_from_descriptor
    from anneal_authoring.perception import snapshot_interactive, snapshot_text_anchors
    from playwright.async_api import async_playwright

    async def _run():
        async with async_playwright() as p:
            br = await p.chromium.launch()
            try:
                page = await (await br.new_context()).new_page()
                await page.set_content('<nav>Private</nav><h1 data-testid="private-content">Members only</h1>'
                                       '<a href="/x">Edit <h2>not an anchor</h2></a>')
                assert await snapshot_interactive(page) and len(await snapshot_interactive(page)) == 1   # the link
                anchors = await snapshot_text_anchors(page)
                assert [(a["tag"], a["role"], a["text"], a["testid"]) for a in anchors] == \
                    [("h1", "heading", "Members only", "private-content")]       # the heading inside the link is a control's
                d = _exact_descriptor(anchors, "Members only")
                assert d is anchors[0]
                t = _target_from_descriptor(d)
                assert t["strategy"] == "testid" and t["selector"] == "private-content"
                assert t["fallbacks"] == {"text": "Members only", "role": "heading"}
            finally:
                await br.close()
    import asyncio
    asyncio.run(_run())


@pytest.mark.m2
def test_exact_descriptor_uses_implicit_role():
    """The assert regrounder keys on the ACCESSIBLE role (a native <a href>/<button> has one without a
    role attribute) — otherwise a plain link is never regrounded and the oracle stays a bare-text rung."""
    from anneal_authoring.author import _exact_descriptor
    raw = [{"tag": "a", "href": "/p/1", "text": "Item abc", "row": {"container_tag": "li", "label": "Item abc"}}]
    assert _exact_descriptor(raw, "Item abc") is raw[0]
    assert _exact_descriptor([{"tag": "div", "text": "Item abc"}], "Item abc") is None


@pytest.mark.m2
def test_resolver_prefers_exact_accessible_name_and_guard_reads_page_title():
    """(1) A role rung's captured name is the FULL accessible name: 'Add' must resolve the 'Add' button,
    not read as ambiguous with 'Add to Favorites' (substring stays the fallback). (2) The destructive
    guard accepts the entity identity from the page title when a page-level confirm names only 'this
    item'. Deterministic — no LLM."""
    from anneal_core.errors import PolicyStop
    from anneal_core.rungs import Rung
    from anneal_executor.guard import guard
    from anneal_executor.resolve import resolve
    from playwright.async_api import async_playwright

    async def _run():
        async with async_playwright() as p:
            br = await p.chromium.launch()
            try:
                page = await (await br.new_context()).new_page()
                await page.set_content('<button aria-label="Add">ADD</button><button>Add to Favorites</button>'
                                       '<button>Create campaign</button>')
                loc, kind = await resolve(page, [Rung("role", "button", name="Add", priority=2)], 2000)
                assert kind == "role" and await loc.get_attribute("aria-label") == "Add"
                loc, _ = await resolve(page, [Rung("role", "button", name="Create", priority=2)], 2000)
                assert await loc.inner_text() == "Create campaign"       # substring fallback still works
                # (2a) a PAGE-LEVEL control (no dialog/row/form ancestor) on a dedicated delete route:
                # the title is the page's own identity → passes
                await page.set_content('<title>Delete "Item tok123xyz"</title>'
                                       '<main><p>This cannot be undone.</p><button>Do it</button></main>')
                await guard(page.get_by_role("button", name="Do it"), "tok123xyz")
                with pytest.raises(PolicyStop):
                    await guard(page.get_by_role("button", name="Do it"), "othertoken")
                # (2b) a dialog that names NO entity on a token-titled detail page: the dialog is the
                # authoritative scope and says nothing about this run's entity → refused
                await page.set_content('<title>Item tok123xyz</title>'
                                       '<dialog open><p>Delete all items?</p><button>Do it</button></dialog>')
                with pytest.raises(PolicyStop):
                    await guard(page.get_by_role("button", name="Do it"), "tok123xyz")
                # (2c) a dialog that DOES name the entity still passes on its own text
                await page.set_content('<title>Items</title>'
                                       '<dialog open><p>Delete "Item tok123xyz"?</p><button>Do it</button></dialog>')
                await guard(page.get_by_role("button", name="Do it"), "tok123xyz")
            finally:
                await br.close()
    import asyncio
    asyncio.run(_run())


@pytest.mark.m2
def test_ground_scope_drops_phantom_container():
    """Layer 1 (PL-7/PL-6): an assert's `scope_role` is committed ONLY when the fully-scoped locator
    resolves on the page — the same locator the cold-replay resolver builds. A container role the app
    does not actually use (a nav grouped by headings, not an ARIA list) is dropped so the bare,
    findable target survives; a real container is kept; an absent target (N1-like) drops the scope
    without changing the red outcome. Deterministic — no LLM, no live app."""
    from anneal_authoring.author import _ground_scope
    from playwright.async_api import async_playwright

    async def _run():
        async with async_playwright() as p:
            br = await p.chromium.launch()
            try:
                page = await (await br.new_context()).new_page()
                # target PRESENT but not inside any role=list (nav = plain divs) → phantom scope dropped
                await page.set_content('<nav><div>Alpha</div><div>Beta</div></nav>')
                assert await _ground_scope(page, "list", "Alpha") is None
                # a real role=list container that holds the target → scope RETAINED
                await page.set_content('<ul role="list"><li>Alpha</li></ul>')
                assert await _ground_scope(page, "list", "Alpha") == {"role": "list"}
                # target absent entirely (a never-created negative) → scope dropped, outcome unchanged
                await page.set_content('<div>nothing here</div>')
                assert await _ground_scope(page, "list", "Alpha") is None
                # the container role used TWICE (sidebar list + content list, both holding the item) does
                # not isolate the target → dropped, so the landmark scope can take over
                await page.set_content('<nav><ul role="list"><li>Alpha</li></ul></nav>'
                                       '<main><ul role="list"><li>Alpha</li></ul></main>')
                assert await _ground_scope(page, "list", "Alpha") is None
                # no scope_role picked → nothing to ground
                assert await _ground_scope(page, None, "Alpha") is None
            finally:
                await br.close()

    asyncio.run(_run())


@pytest.mark.m2
def test_recon_persisted_territory_notes(frozen):
    eng, tid = frozen["engine"], frozen["target_id"]
    with eng.connect() as c:
        row = c.execute(text("select notes from territory_notes where target_id=:t order by version desc "
                             "limit 1"), {"t": tid}).mappings().first()
    assert row is not None, "recon did not persist TerritoryNotes (D25)"
    notes = row["notes"]
    assert "routes" in notes and "recon_sample" in notes
