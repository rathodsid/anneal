"""§7/§10 routing: an error_class ROUTES to a lane. The concrete failure signal beats
any change narrative (the classifier-bias trap) — routing is a pure lookup on the deterministic
error_class the executor already emitted (§7), never an LLM re-reading of the failure.

Infra/env is matched (upstream, in §7) before behavioral, so by the time we route, env classes are
already distinct. A control we resolved but couldn't operate is TOOL_ERROR → engine triage, never the
app's fault — it must not poison the gate.
"""
from __future__ import annotations

from enum import Enum


class Lane(str, Enum):
    HEAL = "heal"            # locator drift → warm ladder/LLM heal (D3/D28)
    REAUTHOR = "reauthor"    # behavioral change → hot re-author → provisional (§6.3/§9)
    REVIEW = "review"        # deterministic API/UI disagreement → review queue (no heal, no re-author)
    PARK = "park"            # environment/auth → retry/park, never the review queue
    STOP = "stop"            # policy/engine fault → hard stop, surface as-is
    NONE = "none"            # not a red we act on (flaky handled upstream)


# error_class → lane. The behavioral classes (postcondition/network) go to re-author, where the
# agent's ability to complete the intent splits BEHAVIOR_CHANGE from PRODUCT_BUG (§9/§10).
_ROUTE = {
    "LOCATOR_DRIFT": Lane.HEAL,
    "POSTCONDITION_UNMET": Lane.REAUTHOR,
    "NETWORK_EXPECTATION_UNMET": Lane.REAUTHOR,
    "API_MISMATCH": Lane.REVIEW,
    "UI_BUG": Lane.REVIEW,
    "ENV_INFRA": Lane.PARK,
    "ENV_CAPACITY": Lane.PARK,
    "AUTH_EXPIRED": Lane.PARK,
    "POLICY_STOP": Lane.STOP,
    "ENGINE_FAULT": Lane.STOP,
    "UNSUPPORTED_VERB": Lane.STOP,
    "TOOL_ERROR": Lane.STOP,       # engine triage — not the app's fault (A12)
    "FLAKY": Lane.NONE,
}


def route(error_class: str | None) -> Lane:
    if not error_class:
        return Lane.NONE
    return _ROUTE.get(error_class, Lane.REVIEW)
