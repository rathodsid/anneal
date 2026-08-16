"""error_class taxonomy (§7) + the typed exceptions the compiler and interpreter raise.

`errors.py` holds the exception→class mapping as *data/logic* (LLD §7): the interpreter's
`_classify(exc, phase)` is this table lookup, not scattered `except` clauses. Infra signatures
(rows 10–12) are matched BEFORE behavioral ones — deterministic replay makes env problems look
like bugs, so the taxonomy catches them first (§7 rule).

These exception types are pure Python (no Playwright, no DB), so both anneal-core (compile) and
anneal-executor (interpret) reference the same classes — keeping anneal-core dependency-clean (D31).
"""
from __future__ import annotations

from enum import Enum


class ErrorClass(str, Enum):
    LOCATOR_DRIFT = "LOCATOR_DRIFT"
    TOOL_ERROR = "TOOL_ERROR"
    POSTCONDITION_UNMET = "POSTCONDITION_UNMET"
    NETWORK_EXPECTATION_UNMET = "NETWORK_EXPECTATION_UNMET"
    API_MISMATCH = "API_MISMATCH"
    UI_BUG = "UI_BUG"
    ENV_INFRA = "ENV_INFRA"
    ENV_CAPACITY = "ENV_CAPACITY"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    POLICY_STOP = "POLICY_STOP"
    ENGINE_FAULT = "ENGINE_FAULT"
    UNSUPPORTED_VERB = "UNSUPPORTED_VERB"
    FLAKY = "FLAKY"


# ── compile-time exceptions (§5.1) ───────────────────────────────────────────
class CompileError(Exception):
    """Base for compiler-stage failures."""


class UnsupportedVerb(CompileError):
    """An action the compiler's registry doesn't support → UNSUPPORTED_VERB (engine gap, D16)."""


class PlaceholderError(CompileError):
    """A `{{…}}` token outside the closed registry (D36 / CR-2)."""


class AssertOracleError(CompileError):
    """`negative` stacked on a visible/hidden state predicate (CR-4) — double negation."""


# ── interpreter-time exceptions (§6/§7) ──────────────────────────────────────
class LocatorUnresolved(Exception):
    """resolve() budget exhausted; no rung reached count()==1 (§7 row 1)."""


class LocatorAmbiguous(Exception):
    """the only matching rung had count()>1 — never gamble on .first() (§7 row 2, ANTI D-8)."""


class PostconditionUnmet(Exception):
    """an element/url/value wait timed out (§7 rows 4–6)."""


class NetworkExpectationUnmet(Exception):
    """an armed expect_response found no match in the window (§7 row 7)."""


class PolicyStop(Exception):
    """the destructive guard refused an off-token action (§7 row 13, D15)."""


class ColdModeViolation(Exception):
    """the cold path reached for an LLM (§7 row 14, D31) — hard red + incident."""


class AuthExpired(Exception):
    """sign-in redirect detected mid-run (§7 row 12)."""


class EngineFault(Exception):
    """the engine itself is broken (a programming error surfaced from a replay) — the engine's fault,
    never the app's. Raised where an ENGINE_FAULT outcome must be re-surfaced LOUD instead of being
    laundered into a red verdict / a 'didn't verify' (§7 row 14)."""


# Python built-in exception types that mean OUR code is broken (never the app's or a tool's) → ENGINE_FAULT.
_ENGINE_BUG_TYPES = (TypeError, AttributeError, KeyError, IndexError, NameError, ZeroDivisionError,
                     AssertionError, NotImplementedError)


# ── the §7 table as a lookup ─────────────────────────────────────────────────
_ENV_INFRA_MARKERS = ("net::err", "err_connection", "err_network", "econnrefused",
                      "econnreset", " 502", " 503", " 504", " 520", "bad gateway")
_ENV_CAPACITY_MARKERS = ("429", "quota", "rate limit", "too many requests", "capacity", "overloaded")


def classify_exception(exc: BaseException, phase: str) -> ErrorClass:
    """Map a raised exception + the phase it fired in to an error_class (§7).

    phase ∈ {compile, navigate, locate, act, verify, any}. Infra/env first (§7 ordering rule),
    then our own typed exceptions, then Playwright signals by name/message.
    """
    msg = str(exc).lower()

    # rows 10–11: infra/env matched BEFORE behavioral (§7)
    if any(m in msg for m in _ENV_INFRA_MARKERS):
        return ErrorClass.ENV_INFRA
    if any(m in msg for m in _ENV_CAPACITY_MARKERS):
        return ErrorClass.ENV_CAPACITY

    # row 12
    if isinstance(exc, AuthExpired):
        return ErrorClass.AUTH_EXPIRED
    # rows 13–15 (engine/policy/compile — deterministic)
    if isinstance(exc, PolicyStop):
        return ErrorClass.POLICY_STOP
    if isinstance(exc, ColdModeViolation):
        return ErrorClass.ENGINE_FAULT
    if isinstance(exc, UnsupportedVerb):
        return ErrorClass.UNSUPPORTED_VERB
    # rows 1–2
    if isinstance(exc, (LocatorUnresolved, LocatorAmbiguous)):
        return ErrorClass.LOCATOR_DRIFT
    # row 7
    if isinstance(exc, NetworkExpectationUnmet):
        return ErrorClass.NETWORK_EXPECTATION_UNMET
    # rows 4–6
    if isinstance(exc, PostconditionUnmet):
        return ErrorClass.POSTCONDITION_UNMET

    # row 3: a Playwright TimeoutError after resolve, during the act phase, is an actionability
    # problem (not the app's fault) → TOOL_ERROR; during verify it is a postcondition miss.
    if type(exc).__name__ == "TimeoutError":
        return ErrorClass.POSTCONDITION_UNMET if phase == "verify" else ErrorClass.TOOL_ERROR

    # a Python programming error is OUR bug, not the app's or a tool's — loud (ENGINE_FAULT), never a
    # laundered red/TOOL_ERROR (so a caller like _verify_green can re-surface it instead of swallowing).
    if isinstance(exc, _ENGINE_BUG_TYPES):
        return ErrorClass.ENGINE_FAULT

    return ErrorClass.TOOL_ERROR
