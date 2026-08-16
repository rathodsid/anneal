"""Postcondition → wait lowering runtime (§5.4). A lowered `Wait` is checked AFTER the action, within
its own budget. No fixed sleeps anywhere (D9) — every wait is a bounded poll on a real condition.
"""
from __future__ import annotations

import asyncio
import re
import time

from anneal_core.errors import PostconditionUnmet

from .resolve import _locator_for, _scope_root, resolve, wait_hidden

# checked-state, resolved across the ways a control exposes it: ARIA (`aria-checked`), native
# (`.checked`), a headless-UI data attribute (`data-state=checked|on`), else unknown (None → never
# satisfies, so a mis-targeted element can't rubber-stamp). Generic web semantics, no app literal.
_CHECKED_JS = """el => {
  const a = el.getAttribute('aria-checked');
  if (a !== null) return a === 'true' ? true : (a === 'false' ? false : null);
  if (typeof el.checked === 'boolean') return el.checked;
  const ds = (el.getAttribute('data-state') || '').toLowerCase();
  if (ds === 'checked' || ds === 'on' || ds === 'active') return true;
  if (ds === 'unchecked' || ds === 'off' || ds === 'inactive') return false;
  return null;
}"""


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


async def _eval_single(loc, spec) -> bool:
    """Read the resolved element and compare per kind. checked → checked-state; attribute → getAttribute;
    text → normalized visible text (exact or substring)."""
    if spec.kind == "checked":
        v = await loc.evaluate(_CHECKED_JS)
        return v is not None and bool(v) == bool(spec.checked)
    if spec.kind == "attribute":
        v = await loc.evaluate("(el, n) => el.getAttribute(n)", spec.attr_name)
        return v == spec.equals
    if spec.kind == "text":
        v = _norm(await loc.inner_text())
        want = _norm(spec.equals)
        return (want in v) if spec.contains else (v == want)
    return False


async def eval_state(page, spec, budget_ms) -> tuple[bool, str | None]:
    """Evaluate a state-observing predicate (checked/attribute/text/count) → (ok, rung_used). Bounded-poll
    (like value_is) so a value that settles after the action is still caught. `count` tallies MATCHES of
    the primary rung within scope (NOT the count()==1 resolver, which would reject any n≠1); the others
    resolve a unique element via the ladder and read it. Shared by the interpreter's postcondition and
    standalone-assert paths and vendored into `anneal export`, so all three evaluate identically."""
    deadline = time.monotonic() + budget_ms / 1000
    if spec.kind == "count":
        root = _scope_root(page, spec.scope)
        rung = spec.rungs[0] if spec.rungs else None
        while True:
            try:
                n = await _locator_for(root, rung).count() if rung else -1
            except Exception:
                n = -1
            if n == spec.count:
                return True, (rung.kind if rung else None)
            if time.monotonic() >= deadline:
                return False, (rung.kind if rung else None)
            await asyncio.sleep(0.1)
    loc, rung = await resolve(page, spec.rungs, budget_ms, spec.scope)   # unique element via the ladder
    while True:
        if await _eval_single(loc, spec):
            return True, rung
        if time.monotonic() >= deadline:
            return False, rung
        await asyncio.sleep(0.1)


async def check_wait(page, w) -> None:
    if w.kind == "url":
        try:
            await page.wait_for_url(re.compile(w.match), timeout=w.budget_ms)
        except Exception as e:                                    # TimeoutError → POSTCONDITION_UNMET
            raise PostconditionUnmet(f"url did not match {w.match!r}: {e}") from e
        return

    if w.kind == "element":
        if w.state == "hidden":
            await wait_hidden(page, w.rungs, w.budget_ms, w.scope)
            return
        loc, _ = await resolve(page, w.rungs, w.budget_ms, w.scope)
        await loc.wait_for(state="visible", timeout=w.budget_ms)
        return

    if w.kind == "value_is":
        loc, _ = await resolve(page, w.rungs, w.budget_ms, w.scope)
        deadline = time.monotonic() + w.budget_ms / 1000
        while time.monotonic() < deadline:
            if (await loc.input_value()) == w.equals:
                return
            await asyncio.sleep(0.1)
        raise PostconditionUnmet(f"value never equalled {w.equals!r}")

    if w.kind in ("checked", "attribute", "text", "count"):
        ok, _ = await eval_state(page, w, w.budget_ms)
        if not ok:
            raise PostconditionUnmet(f"{w.kind} predicate not satisfied within {w.budget_ms}ms")
        return

    raise PostconditionUnmet(f"unknown wait kind {w.kind!r}")
