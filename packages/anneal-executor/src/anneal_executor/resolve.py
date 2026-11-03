"""The rung-priority resolver (D28, ANTI D-8) — vendored verbatim into `anneal export`.

The ladder order (testid → role+name → placeholder → text → css) and the count()==1 uniqueness
gate together give a deterministic, auditable resolution; the compiled `Rung` list (D28) carries
the candidates in priority order, and `.or()` is NOT used (ANTI D-8) — a hand-rolled sweep gives rung priority + `rung_used` telemetry + a strict uniqueness
gate that `.first()` would forfeit. Async Playwright (the interpreter/scheduler are async, LLD §10).
"""
from __future__ import annotations

import asyncio
import time

from anneal_core.errors import LocatorAmbiguous, LocatorUnresolved, PostconditionUnmet

_SWEEP_S = 0.15   # re-sweep interval: a hand-rolled loop forfeits Playwright auto-wait, so we re-sweep


def _scope_root(page, scope):
    """`within`: resolve to the container locator first (A2 row-relative / region scoping)."""
    if scope is None:
        return page
    if scope.testid:
        return page.get_by_test_id(scope.testid)
    if scope.row_label:
        base = page.get_by_role(scope.role) if scope.role else page.get_by_role("row")
        return base.filter(has_text=scope.row_label)
    if scope.role:
        return page.get_by_role(scope.role)
    return page


def _locator_for(root, rung):
    if rung.kind == "testid":
        return root.get_by_test_id(rung.value)
    if rung.kind == "role":
        return root.get_by_role(rung.value, name=rung.name) if rung.name else root.get_by_role(rung.value)
    if rung.kind == "placeholder":
        return root.get_by_placeholder(rung.value)
    if rung.kind == "text":
        return root.get_by_text(rung.value)
    if rung.kind == "css":
        return root.locator(rung.value)
    raise ValueError(f"unknown rung kind {rung.kind!r}")


def _locators_for(root, rung):
    """Priority-ordered candidates for ONE rung. A role rung's name is the FULL accessible name the
    author captured, so an exact match comes first; the substring match stays as its fallback (a short
    name like 'Add' also substring-matches 'Add to Favorites' and would read as ambiguous)."""
    if rung.kind == "role" and rung.name:
        return [root.get_by_role(rung.value, name=rung.name, exact=True), _locator_for(root, rung)]
    return [_locator_for(root, rung)]


async def _count_first(root, rung):
    """(locator, count) of the first candidate of the rung that matches anything (exact before substring)."""
    loc, n = None, 0
    for loc in _locators_for(root, rung):
        try:
            n = await loc.count()
        except Exception:
            n = 0
        if n:
            break
    return loc, n


async def resolve(page, rungs, budget_ms, scope=None):
    """PRESENCE resolver (actions, visible/value oracles): the highest-priority rung with EXACTLY
    one match, swept across the whole ladder every ~150ms until budget. NOT Locator.or() (no
    priority, no telemetry, strict-throws)."""
    deadline = time.monotonic() + budget_ms / 1000
    root = _scope_root(page, scope)
    while time.monotonic() < deadline:
        for rung in rungs:                       # already priority-sorted (§2.3)
            loc, n = await _count_first(root, rung)
            if n == 1:                            # count()==1 gate — never gamble on .first() (ANTI D-8)
                return loc, rung.kind
        await asyncio.sleep(_SWEEP_S)
    raise LocatorUnresolved(str([r.kind for r in rungs]))


async def wait_hidden(page, rungs, budget_ms, scope=None):
    """ABSENCE/hidden resolver (the 'gone' oracle — the case D35 exists for; s13/s14). Passes when,
    within budget, the target resolves to ZERO matches (absent) OR its single match reaches
    state='hidden'. count()>1 on a matching rung = ambiguous → LOCATOR_DRIFT."""
    deadline = time.monotonic() + budget_ms / 1000
    root = _scope_root(page, scope)
    while time.monotonic() < deadline:
        for rung in rungs:                       # priority order
            loc, n = await _count_first(root, rung)
            if n == 0:
                continue                          # this rung sees nothing…
            if n == 1:
                if await loc.is_hidden():
                    return rung.kind              # present but hidden ⇒ satisfied
                break                             # present & visible on the top matching rung ⇒ not gone yet
            raise LocatorAmbiguous(str([r.kind for r in rungs]))   # n>1 ⇒ ambiguous, never guess which
        else:
            return None                           # every rung matched nothing ⇒ absent ⇒ 'gone' satisfied
        await asyncio.sleep(_SWEEP_S)
    raise PostconditionUnmet("still visible")     # timed out still present-and-visible → POSTCONDITION_UNMET
