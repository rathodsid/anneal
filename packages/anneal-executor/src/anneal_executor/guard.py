"""The destructive guard (D15, §6.4) — an executor check on a declared field, not a prompt.

A destructive step may only resolve against an entity whose VISIBLE identity carries this run's
token. Binds all three lanes identically (author, heal, replay); the suite-end sweeper obeys it by
construction.
"""
from __future__ import annotations

from anneal_core.errors import PolicyStop

# walk up to the enclosing entity: the dialog / row / form the control sits in. Returns [text, scoped]:
# `scoped` says whether such an ancestor EXISTS — a dialog/row/form is the authoritative scope.
_ENCLOSING_JS = """el => {
    const scope = el.closest('[role=dialog],[role=alertdialog],dialog,[role=row],tr,form');
    const anc = scope || el.parentElement || el;
    return [(anc.innerText || anc.textContent || ''), !!scope];
}"""


async def _enclosing(locator) -> tuple[str, bool]:
    try:
        text, scoped = await locator.first.evaluate(_ENCLOSING_JS)
        return text or "", bool(scoped)
    except Exception:
        return "", False


async def enclosing_entity_text(locator) -> str:
    """The VISIBLE identity text of the dialog/row/form the control sits in (D15). Exposed so the author
    lane can pick WHICH step is the real destructive one — the token-bearing confirm, not a menu-open."""
    return (await _enclosing(locator))[0]


async def guard(locator, token: str) -> None:
    # Fail CLOSED on a missing token: `"" in anything` is always True, so an unbound token would
    # wave every destructive action through — the exact fail-open D15 exists to prevent.
    if not token:
        raise PolicyStop("destructive guard: no run token bound to this run")
    scope_text, scoped = await _enclosing(locator)
    if token not in scope_text and not scoped:
        # A PAGE-LEVEL control (no dialog/row/form ancestor at all — a dedicated delete route) carries
        # the entity's identity in the document title. A dialog that names no entity is NOT rescued
        # by the title: the dialog is the authoritative scope and it said nothing about this run's entity.
        try:
            scope_text += "\n" + (await locator.page.title() or "")
        except Exception:
            pass
    if token not in scope_text:
        raise PolicyStop("destructive action off-token: run token not in enclosing entity identity")
