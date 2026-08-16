"""Rung derivation (§2.3) — the ladder is *computed* from the Target, then frozen into the plan (D28).

The `(strategy, selector)` primary plus each `fallbacks` key each become a rung; the compiler sorts
them by the canonical priority so the resolver's first-match-wins is priority-correct regardless of
authoring order. `.or()` is explicitly forbidden here (ANTI D-8) — this list drives a hand-rolled,
priority-ordered, telemetry-emitting resolver instead.
"""
from __future__ import annotations

from dataclasses import dataclass

# canonical priority per §2.3 table
_PRIORITY = {"testid": 1, "role": 2, "placeholder": 3, "text": 4, "css": 5}


@dataclass(frozen=True)
class Rung:
    kind: str          # testid | role | placeholder | text | css
    value: str         # the selector / role name / text
    name: str | None = None   # accessible name, for a role rung
    priority: int = 99


def _rung(kind: str, value: str, name: str | None = None) -> Rung:
    return Rung(kind=kind, value=value, name=name, priority=_PRIORITY[kind])


def derive_rungs(target: "TargetLike") -> list[Rung]:
    """target has: strategy, selector, and optional fallbacks (an object with testid/role/text/
    placeholder/css). Returns rungs deduped by (kind,value,name), stable-sorted by priority.

    Row 2 of the §2.3 table: a role rung's accessible *name* comes from the paired `text` signal
    (primary role → fallbacks.text; fallbacks.role → fallbacks.text). Row 4: a `text` rung is built
    from a *bare* fallbacks.text only when there is no fallbacks.role to consume it as a name.
    """
    fb = target.fallbacks
    fb_text = getattr(fb, "text", None) if fb else None
    fb_role = getattr(fb, "role", None) if fb else None

    rungs: list[Rung] = []

    # ── primary ──
    s, sel = target.strategy, target.selector
    if s == "testid":
        rungs.append(_rung("testid", sel))
    elif s == "role":
        rungs.append(_rung("role", sel, name=fb_text))   # name from the paired fallbacks.text
    elif s == "placeholder":
        rungs.append(_rung("placeholder", sel))
    elif s == "text":
        rungs.append(_rung("text", sel))
    elif s == "css":
        rungs.append(_rung("css", sel))

    # ── fallbacks ──
    if fb:
        if getattr(fb, "testid", None):
            rungs.append(_rung("testid", fb.testid))
        if fb_role:
            rungs.append(_rung("role", fb_role, name=fb_text))   # text consumed as the role's name
        if getattr(fb, "placeholder", None):
            rungs.append(_rung("placeholder", fb.placeholder))
        if fb_text and not fb_role:
            rungs.append(_rung("text", fb_text))                 # bare text, no role to consume it
        if getattr(fb, "css", None):
            rungs.append(_rung("css", fb.css))

    # dedup by (kind, value, name), keep first occurrence (authoring order), then stable-sort by priority
    seen: set[tuple[str, str, str | None]] = set()
    deduped: list[Rung] = []
    for r in rungs:
        k = (r.kind, r.value, r.name)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    return sorted(deduped, key=lambda r: r.priority)


# structural typing hint for the docstring/readers; not enforced at runtime
class TargetLike:
    strategy: str
    selector: str
    fallbacks: object | None
