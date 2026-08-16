"""M0 GOLDEN TEST (marker: m0_golden) — the definition of done for M0 (GOAL.md / HLD §15):

the fixture compiles to a step plan and REPLAYS GREEN against the hermetic golden app with
postcondition waits; trace + video captured; `anneal export` prints the matching standalone
Playwright script; zero residue after the run.
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from anneal_executor.evidence import recording
from anneal_executor.export import to_script
from anneal_executor.interpreter import Interpreter
from anneal_artifacts.store import LocalDiskStore

from conftest import GOLDEN_URL, RUN_TOKEN


@pytest.mark.m0_golden
async def test_fixture_compiles_and_replays_green(plan, collecting_sink, tmp_path):
    artifacts = LocalDiskStore(str(tmp_path / "artifacts"))
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        async with recording(browser, artifacts, "golden", base_url=GOLDEN_URL) as (page, ev):
            interp = Interpreter.cold(page, plan, collecting_sink, artifacts,
                                      token=RUN_TOKEN, run_result_id="golden")
            outcome = await interp.run()
        await browser.close()

    # 1. replayed green, every step
    assert outcome.verdict == "green", (
        f"expected green; got {outcome.verdict} ({outcome.error_class}) at {outcome.failed_step_id}")
    assert outcome.steps_run == len(plan.steps)
    assert len(collecting_sink.events) == len(plan.steps)
    assert all(e["status"] == "passed" for e in collecting_sink.events)

    # 2. the network oracle fired (arm-before, §5): POST create + DELETE, both 2xx-matched
    net = [n for e in collecting_sink.events for n in e["network"]]
    methods = {(n["method"], n["matched_expectation"]) for n in net}
    assert ("POST", True) in methods and ("DELETE", True) in methods, f"network oracle: {net}"

    # 3. evidence captured (A9): trace + video on disk
    assert ev.trace_path and Path(ev.trace_path).exists(), "trace not captured"
    assert ev.video_path and Path(ev.video_path).exists(), "video not captured"


@pytest.mark.m0_golden
def test_anneal_export_matches_the_plan(plan):
    script = to_script(plan)
    # compile() (not ast.parse) — it enforces rules ast.parse skips, e.g. `from __future__` position,
    # so a vendored resolver that drags a mid-file future-import in goes red here (not silently runnable).
    compile(script, "anneal_export.py", "exec")
    # the §6.2 resolver is vendored verbatim into the export prelude
    assert "async def resolve" in script and "async def wait_hidden" in script
    # one section per PlanStep, in order — a faithful transcript of cold replay
    sections = [ln for ln in script.splitlines() if ln.startswith("    # [")]
    assert len(sections) == len(plan.steps)
    for i, s in enumerate(plan.steps):
        assert f"    # [{i}] {s.step_id}:" in script
    # the verbs actually used by the plan appear as calls
    assert "await page.goto(" in script                    # goto steps
    assert "robust_click(" in script                       # click steps
    assert "_select_option(" in script                     # the combobox select
    assert "expect_response(" in script                    # the armed network expectations
    # the combobox helper is vendored VERBATIM from actions.py (not a re-simplified copy):
    # `best_len` is the fuzzy best-overlap match only the real _pick_option has.
    assert "best_len" in script


@pytest.mark.m0_golden
async def test_destructive_guard_fails_closed_without_token():
    """D15 fail-closed: an unbound run token must STOP a destructive action, not wave it through
    (`"" in anything` is always True). Checked before the locator is touched, so no page needed."""
    from anneal_core.errors import PolicyStop
    from anneal_executor.guard import guard

    with pytest.raises(PolicyStop):
        await guard(object(), "")


@pytest.mark.m0_golden
async def test_no_residue_after_run(plan, collecting_sink, tmp_path):
    """The created campaign is deleted by the flow (s13): zero orphaned entities remain (D5)."""
    artifacts = LocalDiskStore(str(tmp_path / "artifacts2"))
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        async with recording(browser, artifacts, "residue", base_url=GOLDEN_URL) as (page, ev):
            interp = Interpreter.cold(page, plan, collecting_sink, artifacts,
                                      token=RUN_TOKEN, run_result_id="residue")
            outcome = await interp.run()
        await browser.close()

    assert outcome.verdict == "green"
    with urllib.request.urlopen(f"{GOLDEN_URL}/api/v2/campaigns") as r:
        data = r.read().decode()
    assert f"Campaign {RUN_TOKEN}" not in data, "run left an orphaned campaign (residue)"
