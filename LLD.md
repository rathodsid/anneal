# Anneal — QA Engine LLD

**Companion:** HLD.md is *why*; this is *how*. Every section cites the HLD decision(s) it implements, with the Bundle schema **derived from** `fixtures/campaign-crud.bundle.jsonc`, the normative example. This document adds no decisions — it lowers them to code-level contracts. Where the fixture and the HLD prose disagree, the **fixture wins** (HLD §4).
**Scope:** Chromium-only, one viewport, single default param row, single org (D17, D22, D23).

> **How to read:** §1 is the skeleton (packages + the one dependency rule that makes D31's zero-LLM cold lane auditable). §2–§4 are the three contracts (Bundle JSON, DDL, StepEvent). §5–§7 are the execution core (compiler → interpreter → error classification). §8–§10 are the red path and fleet. §11 is the product surface.

---

## 1. Package layout — `uv` workspace

One repo, one `uv` workspace, one lockfile. Packages are the seams the HLD already drew; the dependency graph is not cosmetic — **it is how D31's "cold has no LLM in its dependency graph" is enforced by the resolver, not by a code review.**

```
anneal/
├── pyproject.toml                 # [tool.uv.workspace] members = ["packages/*"]
├── uv.lock
├── docker-compose.yml             # D34: postgres + engine + golden-app + minio-later
├── alembic.ini
├── schemas/
│   ├── bundle.schema.json         # §2 — the normative Bundle contract (validated at every write)
│   └── step-event.schema.json     # §4
├── fixtures/
│   ├── campaign-crud.bundle.jsonc # the normative example; golden input
│   └── app/                       # golden app — hermetic mock target (no drift toggles; drift is tested against a deliberately-drifted target)
├── packages/
│   ├── anneal-core/               # domain model + compiler. NO playwright, NO db, NO llm, NO http.
│   │   └── src/anneal_core/
│   │       ├── bundle.py          #   Pydantic models mirroring schemas/bundle.schema.json
│   │       ├── stepplan.py        #   the derived, disposable StepPlan dataclasses (§5)
│   │       ├── compile.py         #   Bundle → StepPlan (§5); CompileContext; placeholder registry (D36)
│   │       ├── rungs.py           #   rung derivation from Target primary+fallbacks (§2.3)
│   │       ├── signatures.py      #   step_signature (§9 cluster key input) — module-ref-stable
│   │       ├── errors.py          #   error_class enum + the exception→class table data (§7)
│   │       └── ports.py           #   Protocols: Healer, Author, LlmClient, ArtifactStore, StepEventSink
│   │
│   ├── anneal-executor/           # the interpreter + browser layer. depends: core, playwright.
│   │   └── src/anneal_executor/   #   *** MUST NOT depend on anneal-llm *** (D31, enforced §1.1)
│   │       ├── interpreter.py     #   Interpreter.cold/.warm/.hot (§6)
│   │       ├── resolve.py         #   D28 rung-priority resolver (§6.2) — vendored verbatim into export
│   │       ├── actions.py         #   per-verb impls (A8 behaviors: robust-click, reload-on-miss, …)
│   │       ├── waits.py           #   postcondition → wait lowering runtime (§5.4)
│   │       ├── network.py         #   §5 oracle: arm-before-action, attribution window, templating
│   │       ├── guard.py           #   D15 destructive guard (executor-level, §6.4)
│   │       ├── combobox.py        #   A4 choreography transplant (NUANCES A4)
│   │       ├── evidence.py        #   A9 behaviors: trace/video/screenshot/network-log, always-on
│   │       ├── session.py         #   A10 seeded-session + A5 sign-in detection primitives
│   │       └── export.py          #   `anneal export`: StepPlan → standalone .py (§5.5)
│   │
│   ├── anneal-llm/                # A15+B6 seam: brace-extractor, provider fallback, param quirks
│   ├── anneal-db/                 # SQLAlchemy 2.0 models + Alembic + repositories (§3). depends: core.
│   ├── anneal-artifacts/          # ArtifactStore impls: LocalDiskStore (D34), (MinioStore later)
│   ├── anneal-authoring/          # HOT lane: recon→TerritoryNotes, interview, NL author, calibration
│   │                              #   depends: executor(hot), llm, db. Implements ports.Author.
│   ├── anneal-heal/               # WARM lane: ladder-first heal, drain-all-drifts (D28).
│   │                              #   depends: executor(warm), llm, db. Implements ports.Healer.
│   ├── anneal-classify/           # §7 routing + §9 incident state machine + selection (§11)
│   ├── anneal-scheduler/          # §10: worker pool, SKIP-LOCKED claim, token mint, advisory lock,
│   │                              #   session probe gate, suite-end sweeper
│   ├── anneal-api/                # FastAPI: the ONE API (D32). depends: everything above.
│   └── anneal-cli/                # thin httpx client — everything except rulings (D32)
└── console/                       # static SPA (thin client of anneal-api); incident inbox = D37 mock
```

### 1.1 The dependency rule that makes cold auditable (D31)

`anneal-executor` **does not declare `anneal-llm` as a dependency.** The interpreter reaches intelligence only through the `Healer` / `Author` **Protocols** defined in `anneal_core.ports`. Concrete, LLM-backed implementations live in `anneal-heal` and `anneal-authoring` and are *injected* into `Interpreter.warm(...)` / `Interpreter.hot(...)`. `Interpreter.cold(...)` takes no such parameter and stores `self._healer = None`. Any cold code path that reaches for one hits:

```python
# interpreter.py
def _require_llm(self, why: str):
    if self._temperature is Temp.COLD or self._healer is None:
        raise ColdModeViolation(why)   # → ENGINE_FAULT, hard red + incident (§7)
    return self._healer
```

Because `anneal-llm` is absent from the executor's transitive closure, an accidental `import anneal_llm` in the cold path **fails at import time in CI** (verify.sh asserts it), not at runtime in production. This is HLD §3's "zero-LLM by construction — auditable at one constructor" made literal: one grep gate + one resolver boundary.

### 1.2 Target-agnostic audit

The target-agnostic audit greps `packages/anneal-core` and `packages/anneal-executor` for target literals (hostnames, domain nouns like `campaign`, known testids) and fails on any hit. Target facts live only in `TargetProfile` rows and `TerritoryNotes` (D25). The fixture is test data, not engine code, and is excluded.

---

## 2. The Bundle JSON Schema (derived from the fixture)

Contract source: `fixtures/campaign-crud.bundle.jsonc`. The schema below is `schemas/bundle.schema.json` (JSON Schema draft 2020-12). `anneal_core.bundle` is the Pydantic mirror; **the JSON Schema is the source of truth** and is validated on every DB write of `test_version.bundle` and `module_version.steps` (§3).

### 2.1 Top level

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://anneal.dev/schemas/bundle.schema.json",
  "type": "object",
  "required": ["schema_version","project_id","test_id","version","state",
               "persona","params","lineage","intent","policy","steps"],
  "additionalProperties": false,
  "properties": {
    "schema_version": { "const": 1 },                       // D16 — refuse other versions cleanly
    "project_id":     { "type": "string" },
    "test_id":        { "type": "string" },
    "version":        { "type": "integer", "minimum": 1 },
    "state":          { "enum": ["draft","calibrated","frozen"] },   // D24
    "persona":        { "type": "string" },                 // D20 — one credential binding
    "params":         { "type": "object" },                 // D22 — {{param.x}} slots
    "lineage":        { "$ref": "#/$defs/lineage" },
    "intent":         { "$ref": "#/$defs/intent" },
    "policy":         { "$ref": "#/$defs/policy" },
    "steps":          { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/step" } },
    "calibration":    { "$ref": "#/$defs/calibration" }     // present once state≥calibrated (D7)
  },
  "$defs": {
    "lineage": {
      "type": "object",
      "required": ["parent","reason","ruling","incident","triggered_by","verified_against"],
      "additionalProperties": false,
      "properties": {
        "parent":          { "type": ["integer","null"], "minimum": 1 },
        "reason":          { "enum": ["authored","locator_heal","behavior_change"] },
        "ruling":          { "enum": ["canonical","provisional",null] },
        "incident":        { "type": ["string","null"] },
        "triggered_by":    { "type": "string" },
        "verified_against":{ "type": "string" }             // D13 — a target key; canonical env
      }
    },
    "intent": {
      "type": "object", "required": ["sources","descriptor"], "additionalProperties": false,
      "properties": {
        "descriptor": { "type": "string" },
        "sources": { "type": "array", "minItems": 1, "items": {
          "type": "object", "required": ["kind","verbatim"], "additionalProperties": false,
          "properties": { "kind": { "enum": ["nl","prd","pr","ticket"] },   // D2 — nl only in v1
                          "verbatim": { "type": "string" } } } }
      }
    },
    "policy": {
      "type": "object", "required": ["exclusive","mutating"], "additionalProperties": false,
      "properties": { "exclusive": { "type": "boolean" },   // §8 advisory-lock serialization
                      "mutating":  { "type": "boolean" } }
    }
  }
}
```

### 2.2 Step — action steps and module refs

A `steps[]` entry is **either** a module ref **or** an action step (discriminated: module refs carry `module`, action steps carry `action`).

```jsonc
"step": { "oneOf": [ { "$ref": "#/$defs/module_ref" }, { "$ref": "#/$defs/action_step" } ] },

"module_ref": {                                             // D19 — pin module@version
  "type": "object", "required": ["module"], "additionalProperties": false,
  "properties": { "module": {
    "type": "object", "required": ["ref","version"], "additionalProperties": false,
    "properties": { "ref": { "type": "string" }, "version": { "type": "integer", "minimum": 1 } } } }
},

"action_step": {
  "type": "object",
  "required": ["id","intent","action"],
  "additionalProperties": false,
  "properties": {
    "id":       { "type": "string", "pattern": "^s[0-9]+$" },
    "intent":   { "type": "string" },                       // NL, joined to step events (§4)
    "action":   { "enum": ["goto","click","fill","select","upload","keypress","hover","assert"] }, // D16+D35
    "arg":      { "type": "string" },                       // value for goto/fill/select/keypress/upload
    "target":   { "$ref": "#/$defs/target" },               // required for all but goto
    "assert":   { "$ref": "#/$defs/assert_predicate" },     // ONLY when action=="assert" (§2.5)
    "postcondition": { "oneOf": [ { "$ref": "#/$defs/postcondition" }, { "type": "null" } ] },
    "destructive":   { "type": "boolean" },                 // ◆4 D15 guardrail flag
    "reload_on_miss":{ "type": "boolean" },                 // ◆5 A8 eventual-consistency
    "budget_ms":     { "type": "integer", "minimum": 1 }    // present on assert steps (no postcondition)
  },
  "allOf": [
    { "if": { "properties": { "action": { "const": "assert" } } },
      "then": { "required": ["assert"], "properties": { "postcondition": { "const": null } } },
      "else": { "properties": { "assert": false } } },     // assert field forbidden on non-assert steps
    // three-way, NOT two-way: an assert step's target lives inside `assert.target`,
    // never top-level, so requiring top-level `target` for every non-goto step
    // (the naive `else`) rejects the fixture's own s10/s14. goto→arg; assert→forbid
    // top-level target; everything else→require target.
    { "if": { "properties": { "action": { "const": "goto" } } },
      "then": { "required": ["arg"] },
      "else": {
        "if": { "properties": { "action": { "const": "assert" } } },
        "then": { "properties": { "target": false } },
        "else": { "required": ["target"] } } }
  ]
}
```

**Compile rules the schema can't express** (enforced in `compile.py`, cited where they bite):
- **CR-1 (verbs):** an action the compiler's registry doesn't support → `UNSUPPORTED_VERB`, an *engine gap*, never a test failure (D16).
- **CR-2 (placeholders):** every `{{…}}` token must be in the registry (§2.4) → else compile error (D36).
- **CR-3 (network arming):** a `network` postcondition lowers to a listener armed **before** the action (§5, §5.4).

### 2.3 Target and the rung ladder (D28, NUANCES A1/A6)

```jsonc
"target": {
  "type": "object", "required": ["strategy","selector"], "additionalProperties": false,
  "properties": {
    "strategy": { "enum": ["testid","role","text","placeholder","css"] },
    "selector": { "type": "string" },                       // may carry {{…}} (e.g. row text)
    "within":   { "$ref": "#/$defs/scope" },                // ◆3 A2 row-relative / region scoping
    "fallbacks":{ "$ref": "#/$defs/fallbacks" }             // A6 authoring-captured extra ladder signals
  }
},
"fallbacks": {                                              // a bag of extra rung signals, NOT ordered here
  "type": "object", "additionalProperties": false,
  "properties": {
    "testid":      { "type": "string" },
    "role":        { "type": "string" },
    "text":        { "type": "string" },                    // when paired with role → the accessible name
    "placeholder": { "type": "string" },
    "css":         { "type": "string" }
  }
},
"scope": {                                                  // container to resolve WITHIN before the target
  "type": "object", "minProperties": 1, "additionalProperties": false,
  "properties": {
    "testid":    { "type": "string" },
    "role":      { "type": "string" },
    "row_label": { "type": "string" }                       // A2: the row whose accessible text contains this
  }
}
```

**Rung derivation (`rungs.py`) — the ladder is *computed*, then frozen into the plan (D28).** The `(strategy,selector)` primary plus each `fallbacks` key each become a **rung**; the compiler sorts them by the canonical priority so the resolver's first-match-wins is priority-correct regardless of authoring order:

| priority | rung kind | built from |
|---|---|---|
| 1 | `testid` | `strategy:testid` primary, or `fallbacks.testid` |
| 2 | `role+name` | `strategy:role` primary (name from paired `fallbacks.text`), or `fallbacks.role` (+ `fallbacks.text` as name) |
| 3 | `placeholder` | `strategy:placeholder` primary, or `fallbacks.placeholder` |
| 4 | `text` | `strategy:text` primary, or a bare `fallbacks.text` with no `role` |
| 5 | `css` | `strategy:css` primary, or `fallbacks.css` |

Dedup by (kind, value); ties keep authoring order. Output is `list[Rung]` where `Rung = {kind, value, name?, priority}`. This list is stored verbatim in the StepPlan. **s12 note:** the fixture authors `text` as *primary* by choice — derivation still yields `[role+name(menuitem), text(Delete)]` sorted by priority, and the `count()==1` gate (§6.2) decides at replay which actually resolves. `.or()` is explicitly forbidden here (ANTI D-8).

### 2.4 Placeholder registry (D36) — closed, versioned, compile-time

`compile.py` holds the **only** legal token grammar; a token outside it is a compile error (never a literal that leaks — the frozen-token orphan bug, HLD §4):

| token | resolves to | when |
|---|---|---|
| `{{run.token}}` | the run_task token (§10 mint) | compile (per run) |
| `{{run.today}}` | today in `target.timezone` (D36), rendered with `target.date_format` — a **Python `strftime` pattern** (`%Y-%m-%d`), never a literal `YYYY-MM-DD` | compile (per run) |
| `{{run.today±Nd}}` | `today ± N` days, same tz + `strftime` format (`N` = 1–3 digits) | compile (per run) |
| `{{param.<key>}}` | `params[key]` (v1: the single default row, D22) | compile |

Regex: `^\{\{(run\.token|run\.today([+-]\d{1,3}d)?|param\.[a-z0-9_]+)\}\}$` for a whole-value token, plus an embedded-scan variant for tokens inside a larger string (`"Campaign {{run.token}}"`). Compile substitutes; unknown name → `PlaceholderError` (compile error). **Distinct from network templating** (§5): `{{double}}` = value substitution; `{single}` in a network `path` = a match wildcard, never substituted.

### 2.5 Assert predicate (D35) — and the s14 rule

```jsonc
"assert_predicate": {
  "type": "object", "required": ["target"], "additionalProperties": false,
  "properties": {
    "state":   { "enum": ["visible","hidden"] },            // element predicate (fixture s10/s14)
    "equals":  { "type": "string" },                        // value_is predicate (may carry {{…}})
    "negative":{ "type": "boolean" },                       // C-1 honest oracle: pass = !raw
    "target":  { "$ref": "#/$defs/target" }
  },
  "oneOf": [ { "required": ["state"] }, { "required": ["equals"] } ]  // exactly one predicate kind
}
```

**CR-4 — the s14 compile rule (fixture header ◆1, commit `4f03338`):** `negative` **must never stack on a `state` (visible/hidden) predicate.** `hidden` alone is the "gone" oracle; `state:hidden + negative:true` is a double negation that inverts intent. `compile.py` raises `AssertOracleError` if `negative` is truthy while `state` is present. `negative` is legal only on non-visibility predicates (`equals`, and future kinds) — that is the *entire* reason the flag survives in the schema. Calibration (§7 / HLD §7) is what proves an assert *can* fail; this rule keeps it from being defined into always-passing.

---

## 3. Postgres DDL (HLD §15 entities + concurrency correctness)

`anneal-db`, SQLAlchemy 2.0 + Alembic. **The concurrency-correctness objects live in the DDL from the start, not bolted on later:** the open-incident partial unique index, atomic budget guards, the exclusive advisory lock, and SKIP-LOCKED claiming. Postgres extensions: `pgcrypto` (`gen_random_uuid`).

```sql
-- ── org is implicit/single (D17); project is the top real key. Every entity carries project_id. ──
create table project (
  id          text primary key,                 -- 'golden'
  name        text not null,
  created_at  timestamptz not null default now()
);

create table target (                            -- app environments (D13); TargetProfile lives here
  id           uuid primary key default gen_random_uuid(),
  project_id   text not null references project(id),
  key          text not null,                    -- 'golden' — matches lineage.verified_against
  base_url     text not null,
  timezone     text not null,                    -- D36 placeholder tz
  date_format  text not null default '%Y-%m-%d',   -- Python strftime pattern (D36 resolution), NOT 'YYYY-MM-DD'
  max_parallel int  not null default 4,          -- §8 per-target concurrency cap
  profile      jsonb not null default '{}',      -- host filter, auth cfg, stability flags (A9/B4)
  unique (project_id, key)
);

create table territory_notes (                   -- D25, versioned, human-editable
  id          uuid primary key default gen_random_uuid(),
  target_id   uuid not null references target(id),
  version     int  not null,
  notes       jsonb not null,                    -- routes, widget idioms, conventions, volatile regions
  stale       boolean not null default false,    -- set true by any behavior-incident touching a route
  authored_by text not null,                     -- 'recon' | human id
  created_at  timestamptz not null default now(),
  unique (target_id, version)
);

create table credential (                        -- D20 personas; C4 envelope-encrypted at rest
  id           uuid primary key default gen_random_uuid(),
  project_id   text not null references project(id),
  persona      text not null,                     -- 'admin','member'
  secret_enc   bytea not null,                    -- VAULT_KEY envelope
  unique (project_id, persona)
);

create table session (                           -- D14, one live session per credential
  credential_id      uuid primary key references credential(id),
  storage_state_path text,                        -- ArtifactStore ref to .auth state (A10)
  seeded_at          timestamptz,
  last_validated     timestamptz,
  state              text not null default 'unknown'   -- unknown|valid|expired
);

create table module (                            -- D19
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  ref text not null,                              -- 'login'
  head_version int,
  unique (project_id, ref)
);
create table module_version (
  id uuid primary key default gen_random_uuid(),
  module_id uuid not null references module(id),
  version int not null,
  steps jsonb not null,                           -- same shape as bundle.steps action steps
  lineage jsonb not null,
  state text not null default 'frozen',
  created_at timestamptz not null default now(),
  unique (module_id, version)
);

create table test (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  test_key text not null,                         -- 'campaign-crud'
  head_version int,                               -- current canonical/provisional frozen head
  quarantined boolean not null default false,     -- B9 / D27 oscillation stop
  unique (project_id, test_key)
);

create table incident (                          -- created before test_version for its FK (§9)
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  state text not null default 'open',             -- open|propagating|ruled|closed
  klass text not null,                            -- locator|behavior|env
  ck_surface     text not null,                   -- cluster_key part 1: route / templated endpoint
  ck_step_sig    text not null,                   -- part 2: normalized step signature (§9, module-stable)
  ck_error_class text not null,                   -- part 3
  representative_test_id uuid references test(id),
  change_brief text,
  ruling text,                                    -- feature|bug|null
  ruling_note text,                               -- D37 — one-line why → also copied to changelog
  ruled_by text, ruled_at timestamptz,
  budget_agent_runs int not null default 0,       -- D27 atomic guards below
  budget_llm_calls  int not null default 0,
  cap_agent_runs int not null,
  cap_llm_calls  int not null,
  created_at timestamptz not null default now(),
  closed_at timestamptz
);
-- §9 ATTACH RACE: N parallel reds with the same cluster key converge to ONE open incident.
create unique index incident_open_cluster on incident (project_id, ck_surface, ck_step_sig, ck_error_class)
  where state in ('open','propagating');

create table test_version (                      -- the Bundle (D18), JSONB steps
  id uuid primary key default gen_random_uuid(),
  test_id uuid not null references test(id),
  version int not null,
  bundle jsonb not null,                          -- full Bundle; validated against schemas/bundle.schema.json
  state text not null default 'draft',            -- draft|calibrated|frozen (D24)
  reason text not null,                           -- authored|locator_heal|behavior_change
  parent_version int,
  ruling text,                                    -- canonical|provisional|null
  incident_id uuid references incident(id),
  triggered_by text,
  verified_against text not null,                 -- target.key (D13)
  calibration jsonb,                              -- D7 verdicts
  created_at timestamptz not null default now(),
  unique (test_id, version)
);

create table incident_member (                   -- §9 members[]
  incident_id uuid not null references incident(id),
  test_id uuid not null references test(id),
  red_run_id uuid,                                -- run_result.id (FK added after run_result)
  applied_version int,
  primary key (incident_id, test_id)
);

create table suite (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  name text not null,
  member_test_ids uuid[] not null default '{}',
  unique (project_id, name)
);

create table suite_run (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  suite_id uuid references suite(id),             -- null for ad-hoc selection runs (§11)
  mode text not null,                             -- saved|selection (D21)
  trigger text not null,                          -- manual|api|deploy_hook — NO cron (D21)
  target_id uuid not null references target(id),
  state text not null default 'pending',          -- pending|probing|running|parked|done
  verdict text,                                   -- D29 finite set (§10.3)
  park_reason text,                               -- AUTH_EXPIRED|ENV_CAPACITY|ENV_INFRA
  created_at timestamptz not null default now()
);

-- ── the scheduler's claim unit: one row per (suite_run × test). Token minted on claim (§8, §10). ──
create table run_task (
  id uuid primary key default gen_random_uuid(),
  suite_run_id uuid not null references suite_run(id),
  test_id uuid not null references test(id),
  test_version int not null,
  exclusive boolean not null default false,       -- from bundle.policy.exclusive
  state text not null default 'queued',           -- queued|claimed|running|done|cancelled
  token text,                                     -- §8 alphabet: ^[a-z0-9]{10}$ ; minted on claim
  worker_id text,
  claimed_at timestamptz,
  unique (suite_run_id, test_id),
  unique (token)                                  -- §8: collision handling IS this constraint, not app code
);

create table run_result (
  id uuid primary key default gen_random_uuid(),
  run_task_id uuid not null references run_task(id),
  test_id uuid not null references test(id),
  test_version int not null,
  param_row text not null default 'default',      -- D22
  browser text not null default 'chromium',       -- D23
  viewport text not null default '1280x720',
  environment text not null,                      -- target.key
  verdict text not null,                          -- green|green_with_heals|provisional|red|flaky|parked_*
  error_class text,                               -- §7
  incident_id uuid references incident(id),
  trace_path text, video_path text, network_log_path text, screenshot_path text,   -- ArtifactStore refs (D34)
  pinned boolean not null default false,          -- latest run per test (D12)
  off_canonical boolean not null default false,   -- D13 read-only run
  duration_ms int, started_at timestamptz, finished_at timestamptz
);
alter table incident_member add constraint fk_member_run foreign key (red_run_id) references run_result(id);

create table step_event (                        -- D33: written LIVE by the interpreter, per step
  id bigserial primary key,
  run_result_id uuid not null references run_result(id),
  step_index int not null,
  step_id text not null,                          -- 's8' or module-derived (login/s1)
  intent text,
  action text not null,
  rung_used text,                                 -- testid|role|text|placeholder|css|fallback (D28 telemetry)
  status text not null,                           -- passed|failed|skipped
  error_class text,
  budget_ms int, elapsed_ms int,
  network jsonb,                                  -- §5 attributed requests
  detail jsonb,                                   -- postcondition results, before/after frame refs
  created_at timestamptz not null default now()
);
create index step_event_run_idx on step_event (run_result_id, step_index);

create table changelog (                         -- D3/D37 — permanent; heals + rulings, never pruned
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  test_id uuid, incident_id uuid,
  kind text not null,                             -- locator_heal|behavior_ruling|rollback|rebaseline
  summary text not null,                          -- e.g. 'btn-create-campaign → btn-new-campaign'
  ruling_note text,                               -- D37 mirror of incident.ruling_note for behavior rulings
  created_at timestamptz not null default now()
);
```

### 3.1 The four concurrency primitives (SQL the app actually runs)

**(a) Incident attach — upsert to the partial unique index (§9 step 1):**
```sql
insert into incident (project_id, klass, ck_surface, ck_step_sig, ck_error_class, cap_agent_runs, cap_llm_calls)
values (:p, :k, :s, :sig, :ec, :cap_a, :cap_l)
on conflict (project_id, ck_surface, ck_step_sig, ck_error_class) where state in ('open','propagating')
  do update set state = incident.state            -- no-op touch → returns the existing open incident
returning id, (xmax = 0) as created;              -- created flag: this red opened it vs joined it
```
Then `insert into incident_member ... on conflict do nothing`. Two reds racing for the same cluster: one inserts, the other's insert collides and returns the same `id`. No app-level locking, no double incident.

**(b) Atomic budget guard (D27):**
```sql
update incident set budget_agent_runs = budget_agent_runs + 1
where id = :id and budget_agent_runs < cap_agent_runs
returning budget_agent_runs;                      -- 0 rows ⇒ exhausted ⇒ partial results at inbox
```
Same shape for `budget_llm_calls`. The guard is the `WHERE`; there is no read-then-write window.

**(c) Exclusive serialization — session-scoped advisory lock (§8):** a run spans a live browser, longer than one txn, so the lock is **session-scoped on a dedicated connection**, taken at run start and released at run end:
```sql
select pg_advisory_lock( hashtextextended(:target_key || ':exclusive', 0) );   -- blocks until free
-- ... run the exclusive test to completion ...
select pg_advisory_unlock( hashtextextended(:target_key || ':exclusive', 0) );
```
Non-exclusive tests never take it; exclusive tests serialize per target.

**(d) Worker claim — SKIP LOCKED + token mint (C-3, §8/§10):**
```sql
with picked as (
  select id from run_task
  where suite_run_id = :sr and state = 'queued'
  order by exclusive desc, id                     -- drain exclusives when their lock is free
  for update skip locked
  limit 1)
update run_task t set state='claimed', worker_id=:w, claimed_at=now(),
       token = :minted_token                      -- ^[a-z0-9]{10}$, generated by the claimer
from picked where t.id = picked.id
returning t.*;                                     -- unique(token) makes a collision a retry, not a bug
```

---

## 4. StepEvent schema + transport (D33)

**Transport (D33):** the interpreter writes one `step_event` row **as each step completes** — no `events.jsonl`, no ingest hop. Classification (§7) queries rows; the console's run detail reads them live; a mid-run crash leaves exactly the partial rows that show where it died. Rows prune with the run (D12); the `run_result.verdict` + `error_class` summary survives on the pinned run.

`schemas/step-event.schema.json` (also the shape written to the `step_event` table's typed columns + `detail`/`network` JSONB):

```jsonc
{
  "$id": "https://anneal.dev/schemas/step-event.schema.json",
  "type": "object",
  "required": ["step_index","step_id","action","status"],
  "additionalProperties": false,
  "properties": {
    "step_index": { "type": "integer", "minimum": 0 },
    "step_id":    { "type": "string" },                     // 's8' | 'login/s1' (module-derived, §9)
    "intent":     { "type": "string" },
    "action":     { "type": "string" },
    "rung_used":  { "enum": ["testid","role","text","placeholder","css",null] },   // D28 — null for goto/assert-by-url
    "fallback":   { "type": "boolean" },                    // true ⇒ resolved below rung 1 ⇒ auto re-freeze incident
    "status":     { "enum": ["passed","failed","skipped"] },
    "error_class":{ "type": ["string","null"] },            // §7 enum
    "budget_ms":  { "type": "integer" },
    "elapsed_ms": { "type": "integer" },
    "network":    { "type": "array", "items": {             // §5 attributed to this step's window
      "type": "object",
      "properties": { "method": {"type":"string"}, "url": {"type":"string"},
                      "status": {"type":"integer"}, "matched_expectation": {"type":"boolean"} } } },
    "detail":     { "type": "object" }                      // postcondition results, before/after frame refs, guard notes
  }
}
```

`ports.StepEventSink.emit(run_result_id, event)` is the seam; the DB-backed impl (`anneal-db`) inserts a row; a test double collects in memory for unit tests. `fallback:true` is what auto-opens the re-freeze incident (D28) — the interpreter sets it whenever `rung_used` is not the highest-priority rung available in the plan for that step.

---

## 5. The compiler — Bundle → StepPlan (D31, §5, §9, D36)

`anneal_core.compile.compile(bundle, ctx) -> StepPlan`. Pure function, DB-free, LLM-free, Playwright-free. Called **once per run** at run start (the plan is derived + disposable, HLD §4). Two backends consume the `StepPlan`: the interpreter (§6) and `anneal export` (§5.5) — one compiler, so an exported script can never skew from what runs.

```python
@dataclass(frozen=True)
class CompileContext:
    token: str            # run_task.token
    today: date           # in target.timezone (D36)
    date_format: str
    params: dict[str,str]
    modules: Mapping[tuple[str,int], list[dict]]  # resolved module_version.steps, keyed (ref,version)

@dataclass(frozen=True)
class PlanStep:
    step_id: str          # module steps → 'login/s1'; signature-stable (§9)
    index: int
    intent: str
    action: str
    arg: str | None       # placeholders already substituted
    rungs: list[Rung]     # §2.3 derived+sorted; empty for goto
    scope: Scope | None
    waits: list[Wait]     # lowered postconditions (§5.4)
    arm_before: list[NetExpectation]   # CR-3: network listeners to open BEFORE the action fires
    assert_: AssertSpec | None
    destructive: bool
    reload_on_miss: bool
    budget_ms: int

@dataclass(frozen=True)
class StepPlan:
    bundle_ref: tuple[str,int]         # (test_id, version)
    steps: list[PlanStep]
```

### 5.1 Stages (in order)

1. **Verb check (CR-1).** Every `action` in the compiler's registry → else `UnsupportedVerb(kind)`; the caller records `UNSUPPORTED_VERB` (engine gap, D16), never a test red.
2. **Module inline (D19).** Replace each `{module:{ref,version}}` with `ctx.modules[(ref,version)]`; prefix each inlined step's id with `ref/` so its `step_id` (and thus §9 signature) derives from `(module ref + intra-module id)`, **never inlined position** — incidents keep clustering across module version bumps.
3. **Placeholder resolution (D36 / CR-2).** Scan every `arg`, `selector`, assert `equals`, and network `path`/`response` for `{{…}}`; resolve against `ctx`; unknown token → `PlaceholderError`. Network single-brace `{id}` is **left intact** (a match wildcard, not a value).
4. **Rung derivation (§2.3).** `target` → sorted `list[Rung]`.
5. **Postcondition lowering (§5.4).** `postcondition.all[]` → `waits[]` + `arm_before[]`.
6. **Assert lowering (§2.5 / CR-4).** `assert` → `AssertSpec`; reject `negative` on a `state` predicate (`AssertOracleError`).
7. **Guard flag pass (D15).** `destructive:true` steps carry the flag into the plan; the executor enforces it (§6.4) — the compiler only propagates.

### 5.2 The network oracle at compile time (§5)

For a `network` postcondition, the compiler emits a `NetExpectation{method, path_pattern, status_class, response_predicate?}` into `arm_before` (not `waits`). `path_pattern` keeps `{id}`-style wildcards as a compiled regex (`{id}` → `[^/]+`). The **discriminativeness check** (HLD §7, freeze-time, not per-run) lives in `anneal-authoring`'s calibration, not here — the compiler trusts a frozen bundle.

### 5.3 step_signature (`signatures.py`, §9 cluster input)

`step_signature(plan_step) = normalize(action + primary_rung.kind + primary_rung.value_with_volatiles_stripped + scope_shape)`. Volatiles stripped: `{{…}}` tokens, 4+-digit runs (A3), and network `{id}`. For module steps the signature is computed from the `ref/intra-id` step_id, making it stable across pin bumps (§9 cluster_key part 2).

### 5.4 Postcondition → wait lowering (D9, the fixture's kinds)

| kind (fixture) | lowers to (`waits.py` runtime, §6) |
|---|---|
| `url {match}` | `page.wait_for_url(re.compile(match), timeout=budget)` |
| `element {state:visible, target}` | `resolve(...)` (count==1) then `loc.wait_for(state="visible")` within budget (§6.2) |
| `element {state:hidden, target}` | `wait_hidden(...)` (§6.2) — passes on **zero matches (absent)** or a single hidden match; `count>1` = ambiguous → `LOCATOR_DRIFT` |
| `value_is {equals, target}` | `resolve(...)` then poll `loc.input_value() == equals` within budget |
| `network {method,path,status,response?}` | **arm_before**: `page.expect_response(pred)` context entered *before* the action; asserted after (CR-3) |
| `eventually` / `effect_readback` | reserved (D30) — compiler raises `UnsupportedVerb` if seen in v1 |

`budget_ms` comes from the postcondition (action steps) or the step top-level (assert steps). No fixed sleeps anywhere (D9).

### 5.5 `anneal export` (D31)

`anneal_executor.export.to_script(plan) -> str` emits a standalone, runnable Playwright-Python file: a **verbatim prelude** of the §6.2 `resolve()` function, then one call per `PlanStep`. Because both backends read the same `StepPlan`, the script is a faithful transcript of cold replay — for reading and hand-running, never in the execution path. CLI: `anneal export <test> <version>` (D32).

---

## 6. The step-interpreter — three temperatures (D31, §3)

`anneal_executor.interpreter.Interpreter`. One class; temperature fixed at construction.

```python
class Temp(Enum): COLD="cold"; WARM="warm"; HOT="hot"

class Interpreter:
    @classmethod
    def cold(cls, page, plan, sink, artifacts):                 # NO healer/author param — D31
        return cls(Temp.COLD, page, plan, sink, artifacts, healer=None, author=None)
    @classmethod
    def warm(cls, page, plan, sink, artifacts, *, healer):      # heal + drain (D28)
        return cls(Temp.WARM, page, plan, sink, artifacts, healer=healer, author=None)
    @classmethod
    def hot(cls, page, plan, sink, artifacts, *, author):       # agent drives the loop
        return cls(Temp.HOT, page, plan, sink, artifacts, healer=None, author=author)

    async def run(self) -> RunOutcome: ...
```

### 6.1 Cold loop (the steady state — zero LLM, HLD §3)

```
for step in plan.steps:
    if step.action == 'assert':                                            # standalone predicate step — §2.5
        ok, rung_used = actions.assert_(page, step.assert_, step.budget_ms,#   dispatched like any verb;
                                        reload_on_miss=step.reload_on_miss)#   owns resolve / wait_hidden (§6.2)
        emit step_event(status=ok, rung_used, fallback=_fb(step, rung_used), detail)
        if not ok: classify → error_class; STOP
        continue                                                           # NO resolve/act/waits for asserts
    arm each step.arm_before  (network listeners open BEFORE the action)   # CR-3
    if step.action != 'goto':
        locator, rung_used = resolve(page, step.rungs, step.budget_ms, step.scope)   # §6.2
        if step.destructive: guard(locator, ctx.token)                     # §6.4, D15
    actions.dispatch(step.action)(page, step, locator)  # actions.py — ONE impl per verb (A8/A4 transplants)
    for wait in step.waits: assert wait within budget                      # §5.4
    assert each armed NetExpectation                                       # §5
    emit step_event(status, rung_used, fallback=_fb(step, rung_used), network, detail)   # D33/§4
    on any failure: classify exception → error_class (§7); emit failed event; STOP

def _fb(step, rung_used):   # fallback = resolved below the top rung available for this step (§4)
    return bool(step.rungs) and rung_used is not None and rung_used != step.rungs[0].kind
```
Pure function of `(plan, page-state)` (HLD §3). **`assert` is a verb handler in `actions.py` like every other action — there is no separate trailing `if step.assert_` clause; the loop has exactly one dispatch path per step.** A `fallback=true` event auto-opens a re-freeze incident downstream (§9); it does **not** stop the run — a fallback rung is still a pass. `goto` has empty `step.rungs`, so `_fb` is `False` for it (no IndexError).

### 6.2 The rung-priority resolver (D28, ANTI D-8) — vendored verbatim into export

```python
import time, asyncio

async def resolve(page, rungs, budget_ms, scope=None):
    """PRESENCE resolver (actions, visible/value oracles): highest-priority rung with
    exactly one match, swept across the whole ladder every ~150ms until budget.
    NOT Locator.or() (no priority, no telemetry, strict-throws)."""
    deadline = time.monotonic() + budget_ms/1000
    root = _scope_root(page, scope)          # within: testid/role/row_label → container locator
    while time.monotonic() < deadline:
        for rung in rungs:                   # already priority-sorted (§2.3)
            loc = _locator_for(root, rung)
            if await loc.count() == 1:        # count()==1 gate — never gamble on .first() (ANTI D-8)
                return loc, rung.kind
        await asyncio.sleep(0.15)            # hand-rolled sweep: forfeits auto-wait, so we re-sweep
    raise LocatorUnresolved(rungs)           # → LOCATOR_DRIFT (§7)

async def wait_hidden(page, rungs, budget_ms, scope=None):
    """ABSENCE/hidden resolver (the 'gone' oracle — the case D35 exists for; s13/s14).
    Passes when, within budget, the target resolves to ZERO matches (absent) OR its single
    match reaches state='hidden'. count()>1 on a matching rung = ambiguous → LOCATOR_DRIFT."""
    deadline = time.monotonic() + budget_ms/1000
    root = _scope_root(page, scope)
    while time.monotonic() < deadline:
        for rung in rungs:                   # priority order
            loc = _locator_for(root, rung); n = await loc.count()
            if n == 0:  continue             # this rung sees nothing…
            if n == 1:
                if await loc.is_hidden():  return rung.kind   # present but hidden ⇒ satisfied
                break                        # present & visible on top matching rung ⇒ not gone yet
            raise LocatorAmbiguous(rungs)    # n>1 ⇒ ambiguous → LOCATOR_DRIFT (never guess which)
        else:
            return None                      # every rung matched nothing ⇒ absent ⇒ 'gone' satisfied
        await asyncio.sleep(0.15)
    raise PostconditionUnmet("still visible")  # timed out still-present-and-visible → POSTCONDITION_UNMET (§7)
```
`count()>1` on the only matching rung within a sweep is *not* a resolution — `resolve` keeps sweeping and, on timeout, raises `LocatorUnresolved` (ambiguous fallback fails locator-class rather than gambling — HLD §3 ambiguity rule). The postcondition-as-wait is the backstop against a wrong-element success.

**The assert verb handler (`actions.assert_`, §6.1) dispatches by predicate kind (§2.5):**

```python
async def assert_(page, spec, budget_ms, reload_on_miss):
    root_rungs = (spec.target.scope, spec.target.rungs)
    try:
        if spec.kind == 'element' and spec.state == 'hidden':
            return True, await wait_hidden(page, *root_rungs, budget_ms)          # 'gone'
        if spec.kind == 'element' and spec.state == 'visible':
            loc, rung = await resolve(page, *root_rungs, budget_ms)
            await loc.wait_for(state='visible'); return True, rung
        if spec.kind == 'value_is':
            loc, rung = await resolve(page, *root_rungs, budget_ms)
            raw = (await loc.input_value()) == spec.equals
            return (not raw if spec.negative else raw), rung   # C-1; negative legal ONLY here (CR-4)
    except (LocatorUnresolved, LocatorAmbiguous, PostconditionUnmet):
        if reload_on_miss:                                     # ◆5 eventual consistency: reload once, re-check
            await page.reload(); return await assert_(page, spec, budget_ms, reload_on_miss=False)
        raise                                                  # → classified per §7
```
`negative` never reaches the `state` branches — CR-4 rejected that combination at compile — so the double-negation the s14 fix removed is structurally unreachable at runtime too.

### 6.3 Warm loop — heal + drain all drifts in one session (D28, §3)

Warm cold-replays to the failure point (deterministic), then on a `LOCATOR_DRIFT` **pauses with the browser open**, calls `self._healer.heal(step, page)` (ladder-first from captured fallbacks, LLM-over-DOM only if the ladder fails — NUANCES A13), retries the step, and **continues** — healing s2, s9, s15 in one run, not three rerun cycles. Each heal that verifies red→green becomes a proposed vN+1 (auto-applied per D3, §9). A heal recurring on the same `step_signature` trips the oscillation stop (D27): quarantine + incident, spend nothing. Warm never "attaches to the dead cold browser" — it is always a fresh run (HLD §3).

### 6.4 The destructive guard (D15, §3) — executor-level, not a prompt

```python
def guard(locator, token):
    # destructive steps may only resolve against an entity whose VISIBLE identity carries this run's token
    scope_text = await locator_enclosing_entity_text(locator)   # dialog / row the control sits in
    if token not in scope_text:
        raise PolicyStop("destructive action off-token")         # → POLICY_STOP (§7), run stops
```
Binds all three lanes identically (author, heal, replay). The suite-end sweeper (§10) obeys it by construction.

---

## 7. Playwright-exception → error_class (§10's foundation)

The normative table the whole §10 taxonomy sits on (HLD §10: "one level down… owned by the executor, not scattered `except` clauses"). `errors.py` holds it as data; `interpreter._classify(exc, phase)` is a table lookup, not `if/elif`.

| # | condition / Playwright signal | phase | `error_class` | §10 category → route |
|---|---|---|---|---|
| 1 | `resolve()` budget exhausted, no rung `count()==1` | locate | `LOCATOR_DRIFT` | `LOCATOR_DRIFT` → heal |
| 2 | only matching rung had `count()>1` (ambiguous) | locate | `LOCATOR_DRIFT` | → heal (never gamble) |
| 3 | `TimeoutError`/`"not visible|enabled|stable"` after resolve | act | `TOOL_ERROR` | `TOOL_ERROR` → engine triage (not the app's fault) |
| 4 | `element wait` (visible/hidden) times out | verify | `POSTCONDITION_UNMET` | `BEHAVIOR_CHANGE`/`PRODUCT_BUG` via re-author |
| 5 | `wait_for_url` mismatch | verify | `POSTCONDITION_UNMET` | ″ |
| 6 | `value_is` poll never equal | verify | `POSTCONDITION_UNMET` | ″ |
| 7 | armed `expect_response` — no match in window | verify | `NETWORK_EXPECTATION_UNMET` | red+red → classify normally; green DOM+red net → `API_MISMATCH` (§5) |
| 8 | response matched but status class wrong (5xx/4xx) | verify | `API_MISMATCH` | → review |
| 9 | DOM red + network green | verify | `UI_BUG` | → review |
| 10 | response 5xx / `net::ERR*` / origin timeout | any | `ENV_INFRA` | retry w/ backoff → park |
| 11 | 429 / quota/limit strings | any | `ENV_CAPACITY` | park + suite banner |
| 12 | sign-in redirect (A5 `_is_login_page`) | any | `AUTH_EXPIRED` | re-probe → park (§9 session) |
| 13 | destructive guard refused (D15) | act | `POLICY_STOP` | stop run, surface policy stop |
| 14 | `ColdModeViolation` (LLM requested in cold) | any | `ENGINE_FAULT` | hard red + incident |
| 15 | `UnsupportedVerb` | compile | `UNSUPPORTED_VERB` | engine backlog, not a test red (D16) |
| 16 | pass-on-retry (the one automatic rerun, §6/HLD) | — | `FLAKY` | record flake rate; sustained → quarantine (D27) |

Rule of §10 honored: **infra signatures (10–12) are matched before behavioral ones** — deterministic replay makes env problems look like bugs, so the taxonomy catches them first. The LLM adjudicates only rows the table marks ambiguous (the `BEHAVIOR_CHANGE` vs `PRODUCT_BUG` split, decided by whether the re-author agent can complete the intent, §9).

---

## 8. Incident state machine (§9, D27, D37)

States: `open → propagating → ruled → closed`, plus locator's auto-`closed`. `anneal_classify.incidents`.

```
                     ┌─ klass=locator ─ heal auto-applies per member (D3) ─────────────┐
red run ─ attach(3.1a) ─ open ┤                                                          ├─▶ closed
                     └─ klass=behavior ─ representative re-author ─▶ provisional ─▶ propagating
                                          │  (members guided re-author w/ change_brief; §9.4)
                                          └─ ruling{feature|bug}+ruling_note ─▶ ruled ─▶ closed
```

| from | event | guard | action | to |
|---|---|---|---|---|
| — | red attaches | cluster_key computed (§9) | upsert (3.1a) + add member | `open` |
| open | klass=locator | heal verified red→green | auto-apply vN+1 per member; one `changelog` line | `closed` |
| open | klass=behavior | budget guard (3.1b) passes | re-author **representative** blind → provisional vN+1 + `change_brief` | `propagating` |
| propagating | member drained | budget guard passes each | member re-author w/ `change_brief` injected → provisional | `propagating` |
| propagating | budget exhausted (3.1b returns 0 rows) | — | stop spend; surface **partial** at inbox | `propagating` |
| propagating | **bypass-guard** hit | new path deviates only at failing step, old inert control present | route to **ASK**, don't ship as feature (§6 HLD) | `propagating` |
| propagating/open | ruling `feature` + `ruling_note` | web-only (D32) | all provisional versions → canonical; `changelog(behavior_ruling, note)` | `ruled`→`closed` |
| propagating/open | ruling `bug` + `ruling_note` | web-only | roll back all provisionals; file one bug (C5 shape) carrying all members; `changelog` | `ruled`→`closed` |
| any | oscillation: heal/fallback recurs on same `step_sig` | D27 tripwire | quarantine test; open incident; spend nothing | `open` |

- **Representative** = the first member; `representative_test_id`. Only it re-authors blind (cost shape: 1 discovery + 1 ruling + N guided, HLD §9).
- **`ruling_note` (D37)** is written on `incident` **and** copied into `changelog.ruling_note` — the "why" the next re-author agent (and future-you) reads when the same surface drifts again. A behavior-ruling changelog entry is never a bare feature/bug flag.
- Budgets (`cap_agent_runs`, `cap_llm_calls`) seeded from `TargetProfile`; a per-day LLM ceiling is a separate counter in `anneal-classify` config, checked before any incident spend.

---

## 9. Session probe (D14, §8)

`anneal_executor.session` (primitives, A5/A10) + `anneal_scheduler` (gate). `SuiteRun` starts in `state='probing'`.

- **Pre-suite:** for each distinct `persona` the suite's tests bind (D20), load `base_url`, detect sign-in redirect (A5 URL pattern + password field). On expiry → set `session.state='expired'`, `suite_run.state='parked'`, `park_reason='AUTH_EXPIRED'` **before any run_task is claimed**, and raise a loud operator re-seed alert. No classification, no agent spend (HLD §8's amplification trap closed).
- **Mid-suite:** any `AUTH_EXPIRED`-shaped failure (§7 row 12) re-probes. On confirmed expiry → **cooperative cancellation**: queued `run_task`s → `cancelled`; in-flight workers get a cancel signal and stop at the next step boundary (evidence flushed, `run_result.verdict='parked_auth'`) — never a hard kill leaving half-written rows or orphan browsers.
- Programmatic re-login stays out (D30). `session.last_validated` updated on every successful probe.

---

## 10. Scheduler + fleet (§8, D34, C-3, A16)

`anneal_scheduler`. Worker = an async task holding a browser **context** seeded from the persona's storage state (contexts cheap, sessions scarce — HLD §8). All workers share one process (v1 laptop compose, D34).

### 10.1 Loop
1. Probe gate (§9) — park before claiming if any needed session is expired.
2. Expand: one `run_task` per suite-member test (or per selection pick, §11).
3. Claim via SKIP LOCKED (3.1d); **mint the token on claim** — `^[a-z0-9]{10}$` (lowercase alnum only: regex/`\b`-safe, the underscore lesson B1/HLD §8). `unique(token)` makes a rare collision a re-mint retry, not application logic.
4. Compile (§5) with `CompileContext{token, today-in-target-tz, params, modules}`.
5. Run cold interpreter (§6); write `step_event`s live (D33); on red, retry once (§6/HLD) then classify (§7) and attach to an incident (§9) if not flake/env.
6. Respect `target.max_parallel` (backpressure queue — the fleet never causes the `ENV_CAPACITY` it detects).
7. `exclusive` tests take the session-scoped advisory lock (3.1c) for their whole run; ordered `exclusive desc` so they drain when the lock is free.

### 10.2 Suite-end sweeper (§8, A16, D15)
Deterministic, no LLM: replay templated `DELETE`s against ids the run's **own network log** recorded at create time; where no delete route was observed, fall back to the name-pattern janitor (A16) keyed on this suite run's token. Both paths obey the D15 guard — only token-bearing entities are touched.

### 10.3 SuiteRun verdict (D29 — the CI contract)
Finite set on `suite_run.verdict`: `green · green_with_heals · provisional · red · parked_auth · parked_env`. **`provisional` counts as pass** for deploy gates (annotated). Computed from member `run_result.verdict`s at suite end; a deploy is never blocked awaiting an async ruling.

---

## 11. Product surface — one API, two clients (D32, D37)

`anneal-api` (FastAPI) is the sole engine surface; `anneal-cli` (httpx) and `console/` (static SPA) are thin clients that must not fork behavior. D21's triggers are the same endpoints a CI job calls.

| method + path | purpose | client |
|---|---|---|
| `POST /projects/{p}/tests` | author from NL → starts HOT lane job (fire-and-forget, D24) | console + CLI |
| `GET /jobs/{id}` / `GET /jobs/{id}/questions` | authoring status + **ambiguity interview** (D26) | console (blocking cards) + CLI (terminal prompts) |
| `POST /jobs/{id}/answers` | answer interview questions (unanswered blocks freeze, never guessed) | console + CLI |
| `GET /tests/{id}` · `/tests/{id}/versions` · `/tests/{id}/bundle?v=` | lineage, bundles | both |
| `GET /export/{test}/{version}` | `anneal export` script (§5.5) | CLI |
| `POST /suites/{id}/runs` (`trigger`: manual\|api\|deploy_hook) | launch saved suite | both + CI |
| `POST /runs/select` (NL change description) | §11 selection preview (per-test reason) then launch | console + CLI |
| `GET /runs/{id}` · `/runs/{id}/steps` (live `step_event`s, D33) | run detail: NL intent ↔ frames ↔ network ↔ `rung_used` | both |
| `GET /incidents` · `/incidents/{id}` | the inbox (§9) | console + CLI (read) |
| `POST /incidents/{id}/ruling` `{ruling, ruling_note}` | **web-only** (D32) — CLI does not expose it | **console only** |
| `POST /tests/{id}/rebaseline` | explicit re-baseline (D13) | both |

**Incident inbox** = the D37-locked reference design (mock: `https://claude.ai/code/artifact/2c288ad6-26d9-48a2-a4f0-8d281ab69d41`): promoted real evidence links (trace · video · screenshots for the red run *and* the verify run) above the illustrative frames; every guided member **expands to its own step diff** (a bad guided re-author must not be rubber-stamped by a batch ruling); the sticky ruling bar carries the optional **one-line note field** feeding `ruling_note` (§8). Rulings are the *only* thing the CLI cannot do — they belong in front of screenshots and video, not a terminal.

---

*Companion to HLD.md — every decision there lowered to a code-level contract; no decisions added here. The Bundle schema is derived from `fixtures/campaign-crud.bundle.jsonc`; where the fixture and the prose disagree, the fixture governs.*
