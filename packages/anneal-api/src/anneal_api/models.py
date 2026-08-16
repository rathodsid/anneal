"""Typed API contracts (A0, docs/BACKEND-SPEC.md) — the Pydantic request/response models for every
endpoint. Field names/types mirror the repository projections and the SQL DDL exactly: these models
document the as-built shapes, they do not reshape them. The frontend consumes these via /openapi.json
(generated TypeScript client) and must never hand-write fetch shapes.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

Trigger = Literal["manual", "api", "deploy_hook"]          # D21 — NO cron
Ruling = Literal["feature", "bug"]


# ── runs / launch ─────────────────────────────────────────────────────────────
class LaunchAccepted(BaseModel):
    """B1: launch is async — 202 with the id immediately; watch it via GET /suite_runs/{id} (poll)
    or GET /suite_runs/{id}/events (SSE). A busy target is a 409 carrying the active id."""
    suite_run_id: UUID


class RunListItem(BaseModel):
    """A row of GET /projects/{p}/runs (repositories.list_runs projection; A2 added the Runs-list
    columns — off_canonical/param_row/browser/viewport/test_version/incident_id/trigger)."""
    id: UUID
    test_id: UUID
    test_key: str
    test_version: int
    role: str                                               # B3: primary|retry|heal|verify
    verdict: str
    error_class: str | None = None
    environment: str
    duration_ms: int | None = None
    pinned: bool
    off_canonical: bool
    param_row: str
    browser: str
    viewport: str
    incident_id: UUID | None = None
    trigger: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


class StepEvent(BaseModel):
    """A step_event row (D33): NL intent ↔ atomic action ↔ frames ↔ network.
    `id` is the monotonic poll/SSE cursor (B1) — pass it back as ?after= to get only new rows."""
    id: int
    step_index: int
    step_id: str
    intent: str | None = None
    action: str
    rung_used: str | None = None
    status: str                                             # passed|failed|skipped
    error_class: str | None = None
    budget_ms: int | None = None
    elapsed_ms: int | None = None
    video_offset_ms: int | None = None                      # ms from run-video start; null on non-recorded paths
    network: list[dict[str, Any]] | None = None             # [{method,url,status,matched_expectation}]
    detail: dict[str, Any] | None = None                    # postconditions, before/after frames, fallback


class RunDetail(BaseModel):
    """GET /runs/{id} — the full run_result row + test_key, NL intent fields, and steps.
    `*_path` fields are server-local strings; bytes are served by A1's artifact endpoint."""
    id: UUID
    run_task_id: UUID
    test_id: UUID
    test_version: int
    role: str                                               # B3: primary|retry|heal|verify
    param_row: str
    browser: str
    viewport: str
    environment: str
    verdict: str
    error_class: str | None = None
    incident_id: UUID | None = None
    trace_path: str | None = None
    video_path: str | None = None
    network_log_path: str | None = None
    screenshot_path: str | None = None
    pinned: bool
    off_canonical: bool
    duration_ms: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    test_key: str
    intent_descriptor: str | None = None
    intent_nl: str | None = None
    steps: list[StepEvent] = Field(default_factory=list)


# ── authoring (D24/D26) ───────────────────────────────────────────────────────
class AuthorRequest(BaseModel):
    nl: str
    persona: str = "member"
    answers: dict[str, str] | None = None                   # D26 interview answers (question → answer)


class AuthorNeedsAnswers(BaseModel):
    """The D26 interview is open — ambiguities that would change what the test asserts.
    Answer and re-POST with `answers` merged; unanswered is blocked, never guessed."""
    status: Literal["needs_answers"]
    questions: list[str]


class AuthorFrozen(BaseModel):
    """Frozen + D7-calibrated (proven falsifiable)."""
    status: Literal["frozen"]
    test_id: UUID
    version: int
    calibration: dict[str, Any] | None = None
    steps: int
    bundle: dict[str, Any]
    # RC4 auditability at the seam: steps the author DELIBERATELY dropped as safe over-plan (a
    # redundant auto-persist "save", a transient toast assert) survive to a green freeze and are
    # surfaced here so a caller/importer never mistakes a reduced bundle for a verbatim one. A
    # *failed-to-ground* intended interaction never reaches here — the freeze coverage gate refuses
    # to freeze that (it becomes an AuthorError), so this list only ever carries benign reductions.
    skipped_steps: list[dict[str, Any]] = Field(default_factory=list)


class AuthorError(BaseModel):
    """Authoring failed. The response is streamed (keepalive heartbeats keep a fronting proxy from
    timing out the long browser+LLM author), so the HTTP status is committed before the outcome is
    known — a failure is delivered in-band here instead of as a 5xx. `detail` mirrors the old body."""
    status: Literal["error"]
    detail: str


AuthorResponse = AuthorNeedsAnswers | AuthorFrozen | AuthorError


# ── change-driven selection (§11) ─────────────────────────────────────────────
class SelectRequest(BaseModel):
    change: str                                             # NL description of the change
    launch: bool = False


class SelectionPick(BaseModel):
    """Per-test score + human-readable reason (reason strings are authored server-side)."""
    test_id: UUID
    test_key: str
    score: float
    selected: bool
    cold_start: bool
    reason: str


class SelectLaunched(BaseModel):
    suite_run_id: UUID
    verdict: str | None = None                              # B1: async — null until terminal; poll/SSE


class SelectResponse(BaseModel):
    change: str
    selection: list[SelectionPick]
    selected: list[UUID]
    run: SelectLaunched | None = None                       # present only when launch:true selected tests


# ── incidents (§9, D37) ───────────────────────────────────────────────────────
class Incident(BaseModel):
    """A full incident row. cluster_key = the three ck_* columns; budget = budget_*/cap_* pairs
    (no composite — the UI composes {spent, cap})."""
    id: UUID
    project_id: str
    state: str                                              # open|propagating|ruled|closed
    klass: str                                              # locator|behavior|env
    ck_surface: str
    ck_step_sig: str
    ck_error_class: str
    representative_test_id: UUID | None = None
    change_brief: str | None = None
    rep_concluded: bool
    ruling: str | None = None
    ruling_note: str | None = None
    ruled_by: str | None = None
    ruled_at: datetime | None = None
    budget_agent_runs: int
    budget_llm_calls: int
    cap_agent_runs: int
    cap_llm_calls: int
    created_at: datetime
    closed_at: datetime | None = None


class IncidentMember(BaseModel):
    test_id: UUID
    red_run_id: UUID | None = None
    verify_run_id: UUID | None = None                       # B3: the verify replay that proved the provisional
    applied_version: int | None = None
    ruling: Ruling | None = None                            # A5: the per-member decision, once ruled
    test_key: str
    head_version: int
    quarantined: bool


class ChangelogEntry(BaseModel):
    id: UUID
    project_id: str
    test_id: UUID | None = None
    incident_id: UUID | None = None
    kind: str
    summary: str
    ruling_note: str | None = None
    created_at: datetime


class IncidentDetail(Incident):
    members: list[IncidentMember] = Field(default_factory=list)
    changelog: list[ChangelogEntry] = Field(default_factory=list)


class RulingRequest(BaseModel):
    """WEB-ONLY by convention (D32). Two shapes, exactly one required (A5, money-shot Direction B):
    - `ruling` — uniform: one decision for every member (the "Rule all remaining Feature" bulk action).
    - `members` — per-member: {test_id: feature|bug} for each member with a provisional under this
      incident. One atomic POST; the incident-level ruling becomes derived (feature|bug|mixed).
    feature → that member's provisional becomes canonical; bug → roll back + file a bug."""
    ruling: Ruling | None = None
    members: dict[UUID, Ruling] | None = None
    ruling_note: str | None = None
    ruled_by: str = "web"

    @model_validator(mode="after")
    def _exactly_one_shape(self):
        if (self.ruling is None) == (self.members is None):
            raise ValueError("provide exactly one of 'ruling' (uniform) or 'members' (per-member)")
        if self.members is not None and not self.members:
            raise ValueError("'members' must not be empty")
        return self


class RulingResponse(BaseModel):
    ruling: str                                             # feature|bug|mixed (derived when per-member)
    affected: list[str]                                     # test_keys the ruling propagated to
    ruling_note: str | None = None
    members: dict[UUID, Ruling] | None = None               # per-member decisions as applied (A5)


# ── A2 read-holes: tests, versions, suites, suite_runs, territory, sessions, targets ─────────────
class TestListItem(BaseModel):
    id: UUID
    test_key: str
    head_version: int
    quarantined: bool
    labels: list[str] = []
    # the head version's canonical target KEY (test_version.verified_against). Carried on the catalog
    # read so a client can SHOW which target a label run will use — the derivation itself is the API's
    # (see POST /projects/{p}/labels/{label}/runs), never duplicated per client.
    verified_against: str | None = None


class TestDetail(TestListItem):
    project_id: str


# ── labels: catalog metadata, one flat namespace per project ──────────────────
# Shape-only validation, deliberately: the vocabulary ("smoke", "sanity", …) is the PROJECT's, and a
# label literal must never appear in engine code or prompts (rule 2).
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_LABELS_PER_TEST = 16


def normalize_labels(raw: Iterable[str]) -> list[str]:
    """Trim + lowercase + de-duplicate, order preserved; reject anything off-shape. Raises ValueError
    (→ 422 at the boundary) so a malformed label can never reach storage."""
    out: list[str] = []
    for item in raw:
        label = item.strip().lower()
        if not LABEL_RE.match(label):
            raise ValueError(
                f"invalid label {item!r}: use a–z, 0–9, '-' or '_', starting alphanumeric, "
                f"max 32 chars")
        if label not in out:
            out.append(label)
    if len(out) > MAX_LABELS_PER_TEST:
        raise ValueError(f"too many labels ({len(out)}): max {MAX_LABELS_PER_TEST} per test")
    return out


class TestPatch(BaseModel):
    """Partial update of a test's catalog metadata. `labels` REPLACES the whole set (no add/remove
    verbs — one shape means the client can't race itself into a half-applied edit)."""
    labels: list[str] | None = None

    @model_validator(mode="after")
    def _normalize(self):
        if self.labels is not None:
            self.labels = normalize_labels(self.labels)
        return self


class LabelCount(BaseModel):
    """One row of GET /projects/{p}/labels. No registry table: a label exists exactly as long as some
    test carries it, so this is derived and `test_count` is never stale."""
    label: str
    test_count: int


class TestVersionListItem(BaseModel):
    """One lineage row (test-viewer › Versions)."""
    version: int
    state: str                                              # draft|frozen|provisional|rolled_back|…
    reason: str
    parent_version: int | None = None
    incident_id: UUID | None = None
    ruling: str | None = None
    triggered_by: str | None = None
    created_at: datetime


class TestVersion(TestVersionListItem):
    """A full version: bundle + D7 calibration ("when was this oracle last proven falsifiable")."""
    test_id: UUID
    bundle: dict[str, Any]
    verified_against: str
    calibration: dict[str, Any] | None = None


class Suite(BaseModel):
    id: UUID
    project_id: str
    name: str
    member_test_ids: list[UUID] = Field(default_factory=list)


class SuiteCreate(BaseModel):
    """A4: create/replace a named suite (upserts on (project, name))."""
    name: str
    member_test_ids: list[UUID] = Field(default_factory=list)


class SuitePatch(BaseModel):
    """A4: partial update — omitted fields are left untouched."""
    name: str | None = None
    member_test_ids: list[UUID] | None = None


class PinRequest(BaseModel):
    pinned: bool


class PinResponse(BaseModel):
    id: UUID
    pinned: bool


class SuiteRunResultItem(BaseModel):
    id: UUID
    test_id: UUID
    test_key: str
    verdict: str
    error_class: str | None = None
    duration_ms: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class SuiteRunDetail(BaseModel):
    """GET /suite_runs/{id} — post-hoc inspection, and B1's terminal-state poll target."""
    id: UUID
    project_id: str
    suite_id: UUID | None = None
    mode: str                                               # saved|selection
    trigger: str
    target_id: UUID
    state: str                                              # pending|probing|running|done|parked
    verdict: str | None = None
    park_reason: str | None = None                          # machine code, e.g. AUTH_EXPIRED / ENV_*
    auth_reason: str | None = None                          # verbatim soft-relogin reason (§3.5)
    needs_human: bool = False                               # captcha/OTP/SSO — a human must sign in (G5)
    created_at: datetime
    results: list[SuiteRunResultItem] = Field(default_factory=list)


class Golden(BaseModel):
    """B4: the per-test golden reference recording — pointer + provenance (test-viewer golden panel).
    Bytes are served by GET /tests/{id}/golden/video."""
    test_id: UUID
    source: Literal["auto", "manual"]                       # clean freeze replay | reviewer upload
    reflects_version: int | None = None
    created_at: datetime


class TerritoryNotes(BaseModel):
    """D25: what recon learned about a target (routes, widget idioms, conventions). Latest version."""
    target_id: UUID
    version: int
    notes: dict[str, Any]
    stale: bool
    authored_by: str
    created_at: datetime


class SessionHealth(BaseModel):
    """A persona's seeded target-app login (NOT an Anneal operator): the parked_auth recovery view.
    The storage-state path stays server-side."""
    credential_id: UUID
    persona: str
    state: str                                              # valid|expired|unknown
    seeded_at: datetime | None = None
    last_validated: datetime | None = None


class Target(BaseModel):
    id: UUID
    project_id: str
    key: str
    base_url: str
    timezone: str
    date_format: str
    max_parallel: int
    profile: dict[str, Any] = Field(default_factory=dict)


# ── export ────────────────────────────────────────────────────────────────────
class ExportResponse(BaseModel):
    test_id: UUID
    version: int
    script: str                                             # standalone Playwright .py text
