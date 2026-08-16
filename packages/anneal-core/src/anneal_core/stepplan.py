"""The derived, disposable StepPlan (§5). Compiled once per run; two backends consume it —
the interpreter (§6) and `anneal export` (§5.5) — so an exported script can never skew from
what actually runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .rungs import Rung


@dataclass(frozen=True)
class Scope:
    """A container to resolve WITHIN before the target (A2 row-relative / region scoping)."""
    testid: str | None = None
    role: str | None = None
    row_label: str | None = None   # placeholders already substituted


@dataclass(frozen=True)
class Wait:
    """A lowered postcondition predicate that is checked AFTER the action (§5.4)."""
    kind: str                      # url | element | value_is | checked | attribute | text | count
    budget_ms: int
    match: str | None = None       # url: compiled-regex source
    state: str | None = None       # element: visible | hidden
    equals: str | None = None      # value_is / attribute / text: the expected value
    rungs: list[Rung] = field(default_factory=list)
    scope: Scope | None = None
    attr_name: str | None = None   # attribute: the attribute name to read
    contains: bool = False         # text: substring match instead of exact
    count: int | None = None       # count: the expected number of matches
    checked: bool | None = None    # checked: the expected checked-state


@dataclass(frozen=True)
class NetExpectation:
    """A network listener armed BEFORE the action (CR-3) and asserted after (§5)."""
    method: str
    path_pattern: str              # regex source; `{id}`-style wildcards → `[^/]+`
    status_class: str              # e.g. '2xx'
    budget_ms: int
    response_predicate: dict | None = None


@dataclass(frozen=True)
class AssertSpec:
    """A standalone assert step's predicate (§2.5)."""
    kind: str                      # element | value_is | checked | attribute | text | count
    rungs: list[Rung]
    scope: Scope | None = None
    state: str | None = None       # element: visible | hidden
    equals: str | None = None      # value_is / attribute / text: the expected value
    negative: bool = False
    attr_name: str | None = None   # attribute: the attribute name to read
    contains: bool = False         # text: substring match instead of exact
    count: int | None = None       # count: the expected number of matches
    checked: bool | None = None    # checked: the expected checked-state


@dataclass(frozen=True)
class Capture:
    """Extract a runtime value AFTER a step → a run-scoped var (§ GAP-4). `{{capture.<name>}}` in a
    later step's arg/target/postcondition is resolved at STEP time (not compile), so a create's dynamic
    entity id can drive a later true-backend-reload goto."""
    name: str
    source: str                    # "url" (page.url) | "text" (this step's target text)
    pattern: str                   # regex; group(1) if present, else the whole match


@dataclass(frozen=True)
class PlanStep:
    step_id: str                   # module steps → 'login/s1'; signature-stable (§9)
    index: int
    intent: str
    action: str
    arg: str | None                # compile-time placeholders substituted; {{capture.*}} preserved for runtime
    rungs: list[Rung]              # §2.3 derived+sorted; empty for goto/assert
    scope: Scope | None
    waits: list[Wait]              # lowered postconditions (§5.4)
    arm_before: list[NetExpectation]
    assert_: AssertSpec | None
    destructive: bool
    reload_on_miss: bool
    budget_ms: int
    capture: Capture | None = None


@dataclass(frozen=True)
class StepPlan:
    bundle_ref: tuple[str, int]    # (test_id, version)
    steps: list[PlanStep]


@dataclass(frozen=True)
class CompileContext:
    token: str
    today: date
    date_format: str
    params: dict[str, str]
    # resolved module_version.steps, keyed (ref, version)
    modules: dict[tuple[str, int], list[dict]] = field(default_factory=dict)
