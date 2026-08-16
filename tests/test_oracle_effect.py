"""Regression (M2 hot lane, oracle-falsifiability Prong A): a VALUE-ENTERING step must synthesize a
falsifiable effect-oracle — the value it set is REFLECTED in its control, so a mis-grounded/no-op step
can't freeze green.

Root cause it guards: postcondition synthesis emitted `value_is` only for `fill`; a `select` got NO
postcondition (client-only selects fire no network / no url change), so a wrong or missed selection was
invisible on replay (the silent wrong-agent hole). `synthesize_effect_oracle` is the single seam that
answers "what proves this step did its job" per verb — this locks its contract.

Pure unit test — no browser/DB. Asserts the verb→oracle mapping (and the None cases that must NEVER
become a fabricated/weak oracle, D26).
"""
from __future__ import annotations

import pytest

from anneal_authoring.author import synthesize_effect_oracle

TID = {"strategy": "testid", "selector": "select-outbound-agent", "fallbacks": {"role": "combobox"}}
FILL = {"strategy": "testid", "selector": "input-max-concurrency", "fallbacks": {}}


@pytest.mark.m2
def test_fill_asserts_value_redisplayed():
    o = synthesize_effect_oracle("fill", "1", FILL)
    assert o == {"kind": "value_is", "equals": "1", "target": FILL}


@pytest.mark.m2
def test_select_asserts_option_visible_scoped_to_its_control():
    # a combobox trigger shows its choice as TEXT (not an input value), so the effect-oracle is
    # `element visible` on the option text scoped WITHIN the trigger — falsifiable: absent on a clean
    # (placeholder) or wrong-selection state.
    o = synthesize_effect_oracle("select", "campaign-test-agent", TID)
    assert o["kind"] == "element" and o["state"] == "visible"
    assert o["target"]["strategy"] == "text"
    assert o["target"]["selector"] == "campaign-test-agent"
    assert o["target"]["within"] == {"testid": "select-outbound-agent"}


@pytest.mark.m2
def test_select_without_stable_scope_returns_none_never_a_weak_oracle():
    # a volatile testid (rots per run) or a testid-less control gives no replayable, non-ambiguous
    # anchor — return None (Prong-B / interview surface), never freeze a rubber-stamp (D26).
    assert synthesize_effect_oracle("select", "x", {"strategy": "testid", "selector": "sel-100000"}) is None
    assert synthesize_effect_oracle("select", "x", {"strategy": "role", "selector": "combobox", "fallbacks": {}}) is None


@pytest.mark.m2
def test_no_value_or_non_value_verb_has_no_effect_oracle():
    assert synthesize_effect_oracle("click", None, {"strategy": "testid", "selector": "btn"}) is None
    assert synthesize_effect_oracle("fill", "", FILL) is None
    assert synthesize_effect_oracle("fill", "   ", FILL) is None
    assert synthesize_effect_oracle("hover", "x", TID) is None


# ── seam contract consumed by the agentic loop: post_state grounds the fill oracle in what the field
#    actually REDISPLAYED (an app may reformat the typed value), so replay's exact input_value match holds.
@pytest.mark.m2
def test_fill_grounds_value_is_in_observed_redisplay_when_fed():
    o = synthesize_effect_oracle("fill", "1", FILL, post_state={"value": "1 concurrent"})
    assert o == {"kind": "value_is", "equals": "1 concurrent", "target": FILL}


@pytest.mark.m2
def test_fill_with_no_redisplay_observed_has_no_oracle():
    # app did not redisplay the value (cleared search / masked field) → no value_is that would red on
    # every replay (D26), rather than asserting the intended-but-absent value.
    assert synthesize_effect_oracle("fill", "1", FILL, post_state={"value": None}) is None
    assert synthesize_effect_oracle("fill", "1", FILL, post_state={"value": ""}) is None
