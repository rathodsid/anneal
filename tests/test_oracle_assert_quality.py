"""Regression (M2 hot lane, oracle-falsifiability Prong A′) — the standalone-ASSERT synthesis path must
not freeze a TRANSIENT oracle.

Root cause it guards (2026-07-20 production incident, P4 → LOCATOR_DRIFT): an assert keyed on a success
TOAST (`role=status`) froze green — it is absent on the pre-state (negative control: falsifiable) and
present right after its step (positive replay: green), so the freeze gate cannot see it, yet it DRIFTS on
a later real run once it auto-dismisses. `_assert_target_is_transient` rejects such a target at synthesis
(the assert is RC4-recorded, never a durable oracle). Target-agnostic: keys on ARIA live-region semantics.

Hermetic: data-URL pages. No golden app, no DB, no LLM.
"""
from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import async_playwright

from anneal_authoring.author import _assert_target_is_transient, _reground_assert_target


def _is_transient(html: str, selector: str, *, strategy: str = "text") -> bool:
    async def _run():
        async with async_playwright() as p:
            br = await p.chromium.launch()
            try:
                page = await (await br.new_context()).new_page()
                await page.goto(f"data:text/html,{html}")
                return await _assert_target_is_transient(page, {"strategy": strategy, "selector": selector})
            finally:
                await br.close()
    return asyncio.run(_run())


@pytest.mark.m2
def test_toast_role_status_is_transient():
    assert _is_transient('<div role="status">Saved</div>', "Saved") is True


@pytest.mark.m2
def test_aria_live_region_is_transient_even_via_child():
    # the announcement text usually sits in a CHILD of the aria-live container — the walk must climb.
    assert _is_transient('<div aria-live="polite"><span>Campaign created</span></div>',
                         "Campaign created") is True


@pytest.mark.m2
def test_durable_element_is_not_transient():
    assert _is_transient("<table><tr><td>My Campaign</td></tr></table>", "My Campaign") is False


@pytest.mark.m2
def test_unresolvable_target_is_not_flagged():
    # a target that isn't on the page must NOT be called transient — a never-true oracle is the positive
    # replay's job, not this synthesis check.
    assert _is_transient("<div>Something else</div>", "Ghost") is False


# ── P5: prefer a stable-testid / role+name locator over the LLM's bare-text pick (unique resolution) ──

@pytest.mark.m2
def test_reground_prefers_stable_testid_over_bare_text():
    raw = [{"tag": "a", "testid": "nav-agents", "role": None, "name": None, "text": "Agents", "row": None}]
    t = _reground_assert_target(raw, "Agents")
    assert t["strategy"] == "testid" and t["selector"] == "nav-agents"   # (a text fallback may ride along)


@pytest.mark.m2
def test_reground_prefers_role_name_over_bare_text():
    raw = [{"tag": "a", "testid": None, "role": "link", "name": None, "text": "Agents", "row": None,
            "href": "/agents"}]
    t = _reground_assert_target(raw, "Agents")
    assert t["strategy"] == "role" and t["selector"] == "link"
    assert (t.get("fallbacks") or {}).get("text") == "Agents"


@pytest.mark.m2
def test_reground_keeps_bare_text_when_no_specific_identity():
    # only a plain text node matches (no testid, no explicit/inferable role) → bare text is the honest,
    # unchanged pick (never worse than before).
    raw = [{"tag": "span", "testid": None, "role": None, "name": None, "text": "Agents", "row": None}]
    assert _reground_assert_target(raw, "Agents") == {"strategy": "text", "selector": "Agents"}


@pytest.mark.m2
def test_reground_ignores_volatile_testid_and_loose_matches():
    # a volatile (per-run) testid is not a stable anchor (A3); and a element whose text only CONTAINS the
    # value (not equals) must NOT be matched — a loose match risks the wrong element.
    raw = [{"tag": "div", "testid": "row-91827", "role": None, "name": None,
            "text": "Agents assigned: 3", "row": None}]
    assert _reground_assert_target(raw, "Agents") == {"strategy": "text", "selector": "Agents"}


@pytest.mark.m2
def test_reground_never_targets_an_input_whose_value_matches():
    # Regression: "verify 'Item X' appears" must NOT ground to the SEARCH BOX just
    # because its typed value == "Item X" (a stable testid) — a control's value is not the item
    # appearing. The row that actually displays it has a volatile testid + non-exact text, so the honest
    # pick is bare text (which a table-scope then resolves), NOT the input.
    raw = [
        {"tag": "input", "testid": "input-campaign-search", "role": "searchbox",
         "name": "Search by campaign or agent name", "text": "Item X", "row": None},
        {"tag": "tr", "testid": "row-100000412", "role": "row", "name": None,
         "text": "Item X\n\nDraft\n\ncampaign-test-agent", "row": None},
    ]
    t = _reground_assert_target(raw, "Item X")
    assert t == {"strategy": "text", "selector": "Item X"}       # never the input
