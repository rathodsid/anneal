"""step_signature (§5.3) — the §9 incident cluster-key input, stable across module pin bumps.

signature = normalize(action + primary_rung.kind + primary_rung.value_with_volatiles_stripped
+ scope_shape). Volatiles stripped: {{…}} tokens, 4+-digit runs (A3), network `{id}`. Computed from
the `ref/intra-id` step_id for module steps so it survives version bumps.
"""
from __future__ import annotations

import re

from .stepplan import PlanStep

_TOKEN = re.compile(r"\{\{[^}]+\}\}")     # {{run.token}}, {{param.x}}, …
_DIGITS4 = re.compile(r"\d{4,}")          # 4+-digit runs (per-entity ids, A3)
_BRACE1 = re.compile(r"\{[a-z0-9_]+\}")   # network {id} wildcard


def _strip_volatiles(value: str) -> str:
    value = _TOKEN.sub("§", value)
    value = _BRACE1.sub("§", value)
    value = _DIGITS4.sub("§", value)
    return value.strip().lower()


def _scope_shape(step: PlanStep) -> str:
    sc = step.scope or (step.assert_.scope if step.assert_ else None)
    if not sc:
        return ""
    if sc.testid:
        return "testid"
    if sc.row_label:
        return "row"
    if sc.role:
        return "role"
    return ""


def step_signature(step: PlanStep) -> str:
    primary = step.rungs[0] if step.rungs else None
    if step.assert_ and step.assert_.rungs:
        primary = step.assert_.rungs[0]
    kind = primary.kind if primary else "-"
    value = _strip_volatiles(primary.value) if primary else "-"
    # module-derived step_id (e.g. 'login/s1') → keep only the module-stable prefix shape
    module = step.step_id.split("/")[0] if "/" in step.step_id else ""
    return "|".join([module, step.action, kind, value, _scope_shape(step)])
