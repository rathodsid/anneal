"""Regression (M2 hot lane): the FREEZE COVERAGE GATE — a bundle that silently dropped an intended
interaction must NOT freeze green (RC4 at the freeze boundary).

The witnessing bug (autox-import/ENGINE-ISSUE-BRIEF.md): a create flow whose acts the author could not
ground was still frozen GREEN as a hollow shell. Two compounding gaps produced the silent green:
  1. calibration only ever sees the SURVIVING bundle steps, so dropped acts never affect its
     falsifiability / positive-replay verdict; and
  2. the recorded `skipped_steps` were stripped by the `AuthorFrozen` API model, so no caller saw them.

The generalized fix (target-agnostic, all platforms): the engine already RECORDS every drop with a
`kind` — `ungrounded` (a control the flow named that the engine failed to perform) vs `over_plan` (a
deliberate, safe reduction: a redundant auto-persist 'save', a transient-toast assert). `author_and_freeze`
refuses to freeze when any `ungrounded` drop exists (it re-authors, then fails honestly), while the
benign `over_plan` reductions pass through and are surfaced on the frozen response.

Pure unit tests — no golden app / DB / browser. The live author walk is stubbed.
"""
from __future__ import annotations

import pytest

import anneal_authoring.author as author
import anneal_authoring.service as service
from anneal_authoring.author import (AuthoringIncomplete, _record_ungrounded, author_bundle,
                                      ungrounded_drops)
from anneal_authoring.prompts import ACT_PICK_SYSTEM, SCAFFOLD_SYSTEM


# ── the gate predicate ────────────────────────────────────────────────────────
@pytest.mark.m2
def test_ungrounded_drops_selects_only_failed_interactions():
    """The predicate keeps FAILED intended interactions and excludes the engine's deliberate over-plan
    reductions — so the gate fires on a hollow shell but never on a benign reduction."""
    skips = [
        {"intent": "choose Call Agent", "kind": "ungrounded"},
        {"intent": "save the form", "kind": "over_plan"},
        {"intent": "toast appears", "kind": "over_plan"},
        {"intent": "type the name", "kind": "ungrounded"},
    ]
    dropped = ungrounded_drops(skips)
    assert [d["intent"] for d in dropped] == ["choose Call Agent", "type the name"]


@pytest.mark.m2
def test_ungrounded_drops_empty_when_only_over_plan():
    assert ungrounded_drops([{"intent": "x", "kind": "over_plan"}]) == []
    assert ungrounded_drops([]) == []


@pytest.mark.m2
def test_missing_kind_treated_as_ungrounded():
    """Defensive: a drop recorded without a kind (any future site) is treated as a failed interaction,
    not silently waved through — the gate is fail-closed."""
    assert ungrounded_drops([{"intent": "legacy drop"}]) == [{"intent": "legacy drop"}]


# ── the recorder tags kind ─────────────────────────────────────────────────────
@pytest.mark.m2
def test_record_ungrounded_defaults_to_ungrounded_kind():
    skips: list[dict] = []
    _record_ungrounded({"text": "do a thing"}, "no control", scaffold_flagged=False,
                       skipped_steps=skips, test_id="t")
    assert skips == [{"intent": "do a thing", "destructive": False, "reason": "no control",
                      "kind": "ungrounded"}]


@pytest.mark.m2
def test_record_ungrounded_over_plan_kind_is_carried():
    skips: list[dict] = []
    _record_ungrounded({"text": "save"}, "auto-persist over-plan", scaffold_flagged=False,
                       skipped_steps=skips, test_id="t", kind="over_plan")
    assert skips[0]["kind"] == "over_plan"


@pytest.mark.m2
def test_record_ungrounded_destructive_still_raises():
    """Unchanged: a dropped DECLARED destructive act fails LOUD immediately (never even reaches the
    coverage gate) — the kind-tagging must not weaken this."""
    with pytest.raises(AuthoringIncomplete):
        _record_ungrounded({"text": "delete it"}, "no confirm", scaffold_flagged=True,
                           skipped_steps=[], test_id="t")


# ── author_bundle tags a real failed-ground drop as ungrounded ──────────────────
class _FakePage:
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


@pytest.mark.m2
async def test_author_bundle_records_ungrounded_kind_for_failed_ground(monkeypatch):
    """An interactive step the stub can never ground is RC4-recorded with kind='ungrounded' — the exact
    signal the freeze coverage gate keys on. (Mirrors test_author_goto's harness.)"""
    async def _empty_snapshot(page):
        return []

    async def _no_reveals(page, *a, **k):
        return []

    def _fake_complete_json(system, user, **kw):
        if system is SCAFFOLD_SYSTEM:
            return {"title": "t", "steps": [{"text": "Click Create", "type": "act", "destructive": False}]}
        if system is ACT_PICK_SYSTEM:
            return {"ref": None, "verb": "click", "value": None, "reason": "x"}  # never groundable
        raise AssertionError(f"unexpected LLM call: {system[:40]!r}")

    monkeypatch.setattr(author, "snapshot_interactive", _empty_snapshot)
    monkeypatch.setattr(author, "rank_inventory", lambda *a, **k: [])
    monkeypatch.setattr(author, "inventory_text", lambda *a, **k: "")
    monkeypatch.setattr(author, "_probe_reveals", _no_reveals)
    monkeypatch.setattr(author, "_elem_sig", lambda *a, **k: ())
    monkeypatch.setattr(author, "complete_json", _fake_complete_json)

    res = await author_bundle(_FakePage(), nl="Create a thing", project_id="p", test_id="t",
                              persona=None, target_key="k", start_path="/")
    dropped = [s for s in res.skipped_steps if s["intent"] == "Click Create"]
    assert dropped and dropped[0]["kind"] == "ungrounded"
    # and the gate predicate would flag this bundle as a hollow shell
    assert ungrounded_drops(res.skipped_steps)


# ── the gate in author_and_freeze: hollow → honest fail; over-plan → freezes ────
def _target():
    return {"base_url": "http://localhost", "key": "k", "profile": {"probe_path": "/"}}


def _stub_freeze_env(monkeypatch, *, skipped, call_counter):
    """Stub every seam author_and_freeze touches except the gate under test: no interview, a resolved
    session (so _ensure_session is skipped), a target, a persistable test, and a _author_once that yields
    a bundle plus the given `skipped` drops."""
    monkeypatch.setattr(service, "open_questions", lambda *a, **k: [])
    monkeypatch.setattr(service.repo, "get_target", lambda *a, **k: _target())
    monkeypatch.setattr(service.repo, "get_storage_state", lambda *a, **k: "state.json")
    monkeypatch.setattr(service.repo, "upsert_test", lambda *a, **k: ("test-id", 1))

    async def _fake_author_once(*a, **k):
        call_counter["n"] += 1
        bundle = {"steps": [{"id": "s1"}], "lineage": {}}
        return bundle, {"positive_replay": {"verdict": "green"}}, list(skipped)

    monkeypatch.setattr(service, "_author_once", _fake_author_once)


@pytest.mark.m2
async def test_freeze_gate_rejects_hollow_bundle_after_retries(monkeypatch):
    """An ungrounded drop makes every attempt an _AUTHOR_MISS → re-authored to exhaustion → honest
    AuthoringIncomplete (surfaced to the caller as an AuthorError), NEVER a green freeze."""
    calls = {"n": 0}
    _stub_freeze_env(monkeypatch, skipped=[{"intent": "choose Call Agent", "kind": "ungrounded"}],
                     call_counter=calls)
    with pytest.raises(AuthoringIncomplete) as ei:
        await service.author_and_freeze(engine=object(), project_id="p", test_key="k",
                                        nl="create then cancel", target_id="tid", max_attempts=3)
    assert "freeze coverage gate" in str(ei.value)
    assert "choose Call Agent" in str(ei.value)
    assert calls["n"] == 3          # re-authored on every attempt, not frozen on the first


@pytest.mark.m2
async def test_freeze_gate_allows_over_plan_reduction(monkeypatch):
    """A DELIBERATE over-plan drop (a redundant save / transient toast) is benign — the freeze proceeds
    and the drop is surfaced on the frozen response (RC4 auditability), not gated away."""
    calls = {"n": 0}
    _stub_freeze_env(monkeypatch, skipped=[{"intent": "save the form", "kind": "over_plan"}],
                     call_counter=calls)
    result = await service.author_and_freeze(engine=object(), project_id="p", test_key="k",
                                             nl="fill then save", target_id="tid", max_attempts=3)
    assert result["status"] == "frozen"
    assert result["skipped_steps"] == [{"intent": "save the form", "kind": "over_plan"}]
    assert calls["n"] == 1          # froze on the first attempt — no needless re-author


# ── the API model no longer strips skipped_steps ───────────────────────────────
@pytest.mark.m2
def test_authorfrozen_carries_skipped_steps():
    """The seam bug: AuthorFrozen had no skipped_steps field, so Pydantic silently dropped it from the
    author service's result. It must now round-trip so a caller/importer can audit benign reductions."""
    from uuid import uuid4

    from anneal_api.models import AuthorFrozen

    result = {"status": "frozen", "test_id": uuid4(), "version": 1, "calibration": {}, "steps": 3,
              "bundle": {"steps": []},
              "skipped_steps": [{"intent": "save the form", "kind": "over_plan", "reason": "x"}]}
    frozen = AuthorFrozen.model_validate(result)
    assert frozen.skipped_steps == [{"intent": "save the form", "kind": "over_plan", "reason": "x"}]
    # and it survives serialization back to the HTTP client
    assert "skipped_steps" in frozen.model_dump()
