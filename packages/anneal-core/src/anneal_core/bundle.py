"""Pydantic mirror of schemas/bundle.schema.json (§2).

The JSON Schema is the source of truth (validated on every DB write); this mirror gives the compiler
typed access. `extra="forbid"` mirrors `additionalProperties:false`. Cross-field rules the schema
can't express (CR-4: no `negative` on a state predicate) live in compile.py, not here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fallbacks(_Model):
    testid: str | None = None
    role: str | None = None
    text: str | None = None
    placeholder: str | None = None
    css: str | None = None


class Scope(_Model):
    testid: str | None = None
    role: str | None = None
    row_label: str | None = None


class Target(_Model):
    strategy: Literal["testid", "role", "text", "placeholder", "css"]
    selector: str
    within: Scope | None = None
    fallbacks: Fallbacks | None = None


class AssertPredicate(_Model):
    # `kind` is optional for backward compatibility: when absent, the compiler infers element/value_is
    # from which of `state`/`equals` is present (the original two-oracle grammar). The state-observing
    # kinds (checked/attribute/text/count) name themselves explicitly.
    kind: Literal["checked", "attribute", "text", "count"] | None = None
    state: Literal["visible", "hidden"] | None = None
    equals: str | None = None
    negative: bool | None = None
    target: Target
    name: str | None = None            # attribute: the attribute to read
    contains: bool | None = None       # text: substring match instead of exact
    count: int | None = None           # count: expected number of matches
    checked: bool | None = None        # checked: expected checked-state


class UrlPredicate(_Model):
    kind: Literal["url"]
    match: str


class NetworkPredicate(_Model):
    kind: Literal["network"]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    status: str
    response: dict | None = None


class ElementPredicate(_Model):
    kind: Literal["element"]
    state: Literal["visible", "hidden"]
    target: Target


class ValueIsPredicate(_Model):
    kind: Literal["value_is"]
    equals: str
    target: Target


class CheckedPredicate(_Model):
    kind: Literal["checked"]
    checked: bool
    target: Target


class AttributePredicate(_Model):
    kind: Literal["attribute"]
    name: str
    equals: str
    target: Target


class TextPredicate(_Model):
    kind: Literal["text"]
    equals: str
    contains: bool | None = None
    target: Target


class CountPredicate(_Model):
    kind: Literal["count"]
    count: int
    target: Target


Predicate = (UrlPredicate | NetworkPredicate | ElementPredicate | ValueIsPredicate
             | CheckedPredicate | AttributePredicate | TextPredicate | CountPredicate)


class Postcondition(_Model):
    budget_ms: int
    all: list[Predicate] = Field(min_length=1)


class Capture(_Model):
    name: str
    from_: Literal["url", "text"] = Field(alias="from")
    pattern: str


class ActionStep(_Model):
    id: str
    intent: str
    action: Literal["goto", "click", "fill", "select", "upload", "keypress", "hover", "assert"]
    arg: str | None = None
    target: Target | None = None
    assert_: AssertPredicate | None = Field(default=None, alias="assert")
    postcondition: Postcondition | None = None
    destructive: bool | None = None
    reload_on_miss: bool | None = None
    budget_ms: int | None = None
    capture: Capture | None = None


class Lineage(_Model):
    parent: int | None = None
    reason: Literal["authored", "locator_heal", "behavior_change"]
    ruling: Literal["canonical", "provisional"] | None = None
    incident: str | None = None
    triggered_by: str
    verified_against: str


class IntentSource(_Model):
    kind: Literal["nl", "prd", "pr", "ticket"]
    verbatim: str


class Intent(_Model):
    descriptor: str
    sources: list[IntentSource] = Field(min_length=1)


class Policy(_Model):
    exclusive: bool
    mutating: bool


class Bundle(_Model):
    schema_version: Literal[1]
    project_id: str
    test_id: str
    version: int
    state: Literal["draft", "calibrated", "frozen"]
    persona: str
    params: dict
    lineage: Lineage
    intent: Intent
    policy: Policy
    steps: list[dict]          # raw step dicts; compile.py discriminates module-ref vs action-step
    calibration: dict | None = None
