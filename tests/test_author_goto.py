"""Regression (M2 hot lane): a `goto` pick with no target path must FAIL HONESTLY, never launder into
`start_path`.

Root cause it guards: ACT_PICK can misclassify an interactive step ("Click Create") as
`verb=goto, value=null`. The old `path = value or start_path` collapsed that to the start path, which is
always seeded in `observed_paths` — so the malformed pick sailed past the B3 anti-fabrication guard and
froze as `goto <start>` with a self-satisfying URL postcondition: a green-but-wrong test that navigates
home and leaves every later step running on the wrong page.

The engine has since moved on (`fa11648`): a value-less goto is no longer a navigation at all — with no
observed section it is ACT_PICK PUNTING (a control it could not see), so it FALLS THROUGH to the in-page
active-seek path. If seeking still cannot ground it, the step is RC4-recorded (auditable, surfaced on
freeze), never frozen. Either way the anti-laundering invariant holds: an empty-value goto never produces
a `goto <start>` step. These tests assert that CURRENT contract.

Pure unit test — no golden app / DB. The live page + LLM are stubbed so the ACT_PICK output is
deterministic; everything upstream of the goto branch is a no-op.
"""
from __future__ import annotations

import pytest

import anneal_authoring.author as author
from anneal_authoring.author import AuthoringIncomplete, author_bundle
from anneal_authoring.prompts import ACT_PICK_SYSTEM, SCAFFOLD_SYSTEM


class _FakePage:
    """Minimal async page for the author's recon/act loop. Beyond goto/.url/.on(), a punted-goto now
    reaches the active-seek path (wait_for_timeout + scroll evaluate) and the post-loop teardown
    (remove_listener); evaluate returns a stable interactive-count so await_settled settles fast."""
    url = "http://localhost/"

    async def goto(self, *a, **k):
        return None

    def on(self, *a, **k):
        return None

    def remove_listener(self, *a, **k):
        return None

    async def wait_for_timeout(self, *a, **k):
        return None

    async def evaluate(self, *a, **k):
        return 1


def _stub_liveness(monkeypatch, *, act_pick):
    """Neutralize every live-page/LLM touchpoint upstream of the goto branch. `act_pick` is the ACT_PICK
    JSON to return; the scaffold yields a single interactive step so the loop reaches that branch."""
    async def _empty_snapshot(page):
        return []

    async def _no_reveals(page, *a, **k):
        return []

    def _fake_complete_json(system, user, **kw):
        if system is SCAFFOLD_SYSTEM:
            return {"title": "t", "steps": [{"text": "Click Create", "type": "act", "destructive": False}]}
        if system is ACT_PICK_SYSTEM:
            return act_pick
        raise AssertionError(f"unexpected LLM call: {system[:40]!r}")

    monkeypatch.setattr(author, "snapshot_interactive", _empty_snapshot)
    monkeypatch.setattr(author, "rank_inventory", lambda *a, **k: [])
    monkeypatch.setattr(author, "inventory_text", lambda *a, **k: "")
    monkeypatch.setattr(author, "_probe_reveals", _no_reveals)
    monkeypatch.setattr(author, "_elem_sig", lambda *a, **k: ())
    monkeypatch.setattr(author, "complete_json", _fake_complete_json)


@pytest.mark.m2
@pytest.mark.parametrize("empty_value", [None, ""], ids=["null", "empty-string"])
async def test_empty_value_goto_never_launders_into_start(monkeypatch, empty_value):
    """The witnessing bug: an interactive step ("Click Create") misread as `goto` with an empty value.
    Under the current engine it is not a navigation — with no observed section it falls through to the
    in-page seek; the stub yields nothing groundable, so the step is RC4-recorded, NOT frozen. The
    anti-laundering invariant: the bundle must contain ONLY the initial open step — never a second
    `goto <start>`. The malformed step is surfaced honestly in skipped_steps, never silently dropped.
    None and '' take the identical falsy path — parametrized so both remain witnessed."""
    _stub_liveness(monkeypatch, act_pick={"ref": None, "verb": "goto", "value": empty_value, "reason": "x"})
    res = await author_bundle(_FakePage(), nl="Create a thing", project_id="p", test_id="t",
                              persona=None, target_key="k", start_path="/")
    # ONLY the seeded "open the app" step survives — the empty goto was never laundered into a nav step.
    assert [s["action"] for s in res.bundle["steps"]] == ["goto"]
    assert res.bundle["steps"][0]["arg"] == "/"
    # …and the malformed step is recorded, not silently swallowed (RC4: never a silent skip).
    assert any(s["intent"] == "Click Create" for s in res.skipped_steps)


@pytest.mark.m2
async def test_goto_to_unobserved_path_still_fails(monkeypatch):
    """The pre-existing B3 guard is intact: a non-empty path never observed on the app still fails
    (proves the new empty-value check did not shadow the observed-path check)."""
    _stub_liveness(monkeypatch, act_pick={"ref": None, "verb": "goto", "value": "/nope", "reason": "x"})
    with pytest.raises(AuthoringIncomplete) as ei:
        await author_bundle(_FakePage(), nl="Create a thing", project_id="p", test_id="t",
                            persona=None, target_key="k", start_path="/")
    assert "never observed" in str(ei.value)
