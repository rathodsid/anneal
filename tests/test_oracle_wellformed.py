"""Regression (M2 hot lane, oracle-falsifiability Prong B) — calibration must prove an oracle is
falsifiable AND well-formed AND stable, not merely that it "didn't pass" on the clean pre-state.

Two calibration gate holes this locks (both from the 2026-07-20 production incident):

- **Well-formed (check #1, P2 → TOOL_ERROR):** a `value_is` whose target resolved to a combobox `<div>`
  (not an input) makes `input_value()` THROW on every state. The old blanket `except → failed_on_pre_state`
  miscounted that throw as "correctly could not find it" → the malformed oracle froze and only errored in
  prod. The fix CLASSIFIES the exception: a resolution miss / visibility timeout is honest absence (good);
  any other throw is executor-incompatible → hard `CalibrationError`.
- **Stable & unique on a positive replay (check #3, P4/P5 → LOCATOR_DRIFT):** the negative control only
  proves an oracle is ABSENT on the pre-state — an oracle that is falsifiable yet NEVER TRUE on a real run
  (or resolves ambiguously) still passed. calibration now runs the flow once for real and requires GREEN.

Hermetic: data-URL pages + hand-built bundles. No golden app, no DB, no LLM.
"""
from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import async_playwright

from anneal_authoring.calibrate import CalibrationError, calibrate


def _bundle(start_url: str, steps: list[dict]) -> dict:
    """A minimal frozen-shape bundle (non-mutating, so the negative control runs on the page as-is)."""
    return {
        "schema_version": 1, "project_id": "p", "test_id": "t", "version": 1, "state": "draft",
        "persona": "member", "params": {},
        "lineage": {"reason": "authored", "triggered_by": "test", "verified_against": "test"},
        "intent": {"descriptor": "verify", "sources": [{"kind": "nl", "verbatim": "verify"}]},
        "policy": {"exclusive": False, "mutating": False},
        "steps": [{"id": "s0", "intent": "open", "action": "goto", "arg": start_url}, *steps],
    }


def _calibrate(start_url: str, steps: list[dict]):
    bundle = _bundle(start_url, steps)

    async def _run():
        async with async_playwright() as p:
            br = await p.chromium.launch()
            try:
                return await calibrate(br, None, bundle, start_url)
            finally:
                await br.close()
    return asyncio.run(_run())


def _assert(kind_kw: dict, target: dict) -> dict:
    return {"id": "s1", "intent": "verify", "action": "assert",
            "assert": {**kind_kw, "target": target}, "reload_on_miss": False, "budget_ms": 3000}


@pytest.mark.m2
def test_value_is_on_non_input_is_rejected_as_malformed():
    # check #1: value_is → input_value() on a <div> THROWS (not an input). The classification must NOT read
    # that throw as honest falsification — it is a malformed oracle → hard fail (the P2 witness).
    with pytest.raises(CalibrationError) as ei:
        _calibrate("data:text/html,<div>ChosenAgent</div>",
                   [_assert({"equals": "ChosenAgent"}, {"strategy": "text", "selector": "ChosenAgent"})])
    assert "malformed oracle" in str(ei.value)


@pytest.mark.m2
def test_well_formed_flow_calibrates_green():
    # check #1 (no misfire) + check #3 (positive green): a value_is on a real <input> that a FILL step sets.
    # Negative control skips the fill → the input is still empty → the assert fails on the pre-state
    # (falsifiable). Positive replay runs the fill → the input shows the value → the assert holds → green.
    calib = _calibrate(
        'data:text/html,<input id="agent" value="">',
        [{"id": "s1", "intent": "type", "action": "fill", "arg": "right",
          "target": {"strategy": "css", "selector": "#agent"}},
         _assert({"equals": "right"}, {"strategy": "css", "selector": "#agent"}) | {"id": "s2"}])
    assert calib["s2"]["negative_control"] == "failed_on_pre_state"
    assert calib["positive_replay"]["verdict"] == "green"


@pytest.mark.m2
def test_never_true_oracle_is_rejected_by_positive_replay():
    # check #3: an oracle that is ABSENT on the pre-state (so the negative control rates it falsifiable) but
    # is ALSO never true on a real run is useless — a green replay would be meaningless. The positive replay
    # catches what the negative-only gate could not.
    with pytest.raises(CalibrationError) as ei:
        _calibrate("data:text/html,<div>Something</div>",
                   [_assert({"state": "visible"}, {"strategy": "text", "selector": "Ghost"})])
    assert "positive replay not green" in str(ei.value)
