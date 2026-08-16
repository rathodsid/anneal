"""The WARM-lane Healer (LLD §6.3). Injected into `Interpreter.warm(...)` via ports.Healer.

`heal(step, page)` runs when a step RED with LOCATOR_DRIFT (the whole frozen ladder is dead — a
surviving fallback rung would have passed cold, §6.1). Two stages, ladder-FIRST:
  1. ladder retry — re-resolve the step's captured rungs once (cheap; covers a transient miss).
  2. LLM-over-DOM — observe the live inventory and pick the ref that matches the step's intent from the
     OBSERVED closed vocabulary (B3 index-pick — never a free-generated selector), rebuild a testid-first
     target (A6/A3), and VERIFY it resolves to exactly one element before proposing it.
Only a verified re-resolution becomes a heal; an unverified guess is never shipped (A13).
"""
from __future__ import annotations

from dataclasses import dataclass

from anneal_authoring.author import _target_from_descriptor, _target_ns, _scope_ns
from anneal_authoring.perception import INVENTORY_K, inventory_text, rank_inventory, snapshot_interactive
from anneal_core.errors import LocatorAmbiguous, LocatorUnresolved
from anneal_core.rungs import derive_rungs
from anneal_executor.resolve import resolve
from anneal_executor.session import is_login_page
from anneal_executor.settle import await_settled
from anneal_llm.client import complete_json

HEAL_PICK_SYSTEM = """A frozen web-test step can no longer find its element — the locator rotted. From
the INVENTORY of currently-visible interactive elements (each with an index), pick the ONE index that
is the same control the step means to act on, judging by the step's intent and prior descriptor.
Return ONLY JSON: {"ref": <index>, "reason": "<short>"}. If nothing matches, return {"ref": null}."""


@dataclass
class HealOutcome:
    target: dict            # the rebuilt Target (testid-first, A6 fallbacks) — persisted into vN+1
    rungs: list             # derived ladder for the immediate retry
    summary: str            # "s2: testid→role (re-observed 'Save')"
    old_primary: str
    new_primary: str


def _describe(step) -> str:
    tgt = step.rungs[0] if step.rungs else None
    kind = tgt.kind if tgt else "-"
    val = tgt.value if tgt else "-"
    return f"intent={step.intent!r} was={kind}:{val!r} action={step.action}"


class LadderHealer:
    """ports.Healer. `model` optional (defaults to the seam's top-tier Claude, GOAL.md)."""

    def __init__(self, *, model: str | None = None):
        self._model = model

    async def heal(self, step, page) -> HealOutcome | None:
        # SPA-readiness: the drift may be nothing more than an un-hydrated / mid-redirect page. Settle
        # before re-observing so the ladder retry and the LLM index-pick see the real DOM — never a
        # transient shell or the wrong page (which would VERIFY against that shell and freeze a wrong
        # locator into vN+1). If we settled onto a sign-in wall, this is an expired session, not drift:
        # decline to heal and let the run's AUTH_EXPIRED classification stand.
        await await_settled(page)
        if await is_login_page(page):
            return None

        # 1. ladder-first: a cheap re-resolve of the captured rungs (transient miss / late render).
        if step.rungs:
            try:
                _, rung = await resolve(page, step.rungs, 2000, step.scope)
                return HealOutcome(target=None, rungs=step.rungs, summary=f"{step.step_id}: ladder re-resolved",
                                   old_primary=step.rungs[0].kind, new_primary=rung)
            except (LocatorUnresolved, LocatorAmbiguous):
                pass

        # 2. LLM-over-DOM: observe → index-pick → rebuild target → VERIFY before proposing.
        # Rank by the step's intent so the drifted control stays within the LLM budget on a dense page.
        inv = rank_inventory(await snapshot_interactive(page), step.intent, k=INVENTORY_K)
        if not inv:
            return None
        pick = complete_json(HEAL_PICK_SYSTEM,
                             f"Step: {_describe(step)}\n\nINVENTORY:\n{inventory_text(inv)}",
                             **({"model": self._model} if self._model else {}))
        ref = pick.get("ref")
        if ref is None or not isinstance(ref, int) or ref < 0 or ref >= len(inv):
            return None
        target = _target_from_descriptor(inv[ref])
        rungs = derive_rungs(_target_ns(target))
        try:
            _, rung = await resolve(page, rungs, 4000, _scope_ns(target))   # VERIFY it now resolves
        except (LocatorUnresolved, LocatorAmbiguous):
            return None
        old = step.rungs[0].kind if step.rungs else "-"
        return HealOutcome(target=target, rungs=rungs,
                           summary=f"{step.step_id}: {old}→{target['strategy']} (re-observed)",
                           old_primary=old, new_primary=target["strategy"])
