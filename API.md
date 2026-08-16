# API.md — Anneal backend surface (as-built)

> Accurate to the code at HEAD. **The API is typed (Wave A shipped 2026-07-16, `design/BACKEND-SPEC.md`):**
> every endpoint has Pydantic request/response models (`packages/anneal-api/src/anneal_api/models.py`),
> handlers live in `APIRouter`s (`anneal_api/routers/`), and **`/openapi.json` is the machine-readable
> contract** — the frontend consumes it via a generated TypeScript client (`frontend/`, `npm run gen:api`);
> never hand-write fetch shapes. `create_app(engine=None, artifacts=None)` builds the app;
> `app = create_app()` is the ASGI app. The `tests/test_api_contract.py` gate pins the exact surface
> below — **adding/changing an endpoint means updating the model, the gate, and this file together.**

Base URL (dev): `http://localhost:8080`.
CORS: env `ANNEAL_CORS_ORIGINS` (comma-separated allowlist, for the Vite dev server). Prod serves the
built frontend same-origin from `ANNEAL_FRONTEND_DIST` (explicit opt-in); until then `/` serves the
transitional v0 console page (not part of the contract).
Auth: **none** (see §Auth).

---

## Launch + authoring

### `POST /projects/{project}/suites/{suite_id}/runs` — launch a suite run **(async, B1)**
- Query: `target_id` (**required**), `trigger` (default `api`; `manual|api|deploy_hook` — 422 otherwise; no cron, D21).
- **202 Accepted immediately** with `LaunchAccepted` `{suite_run_id}`; `run_suite` executes on a
  dedicated thread with its own event loop. Watch it via `GET /suite_runs/{id}` (poll to
  `state ∈ done|parked`) or `GET /suite_runs/{id}/events` (SSE).
- **409** if the target already has an active suite_run (index-enforced launch lock) — body
  `{detail: {error: "target busy", active_suite_run_id}}`.
- A crash/API-restart mid-run is reaped at startup: `state=parked, verdict=interrupted,
  park_reason=INTERRUPTED` (distinct from ENV_* — the env wasn't down).

### `POST /projects/{project}/tests` — author a test from NL
- Query: `test_key` (required), `target_id` (required), `labels` (optional, repeatable — applied to the
  frozen test; **422 before any authoring work** if a label is off-shape). Caller-supplied only: the
  author prompts never assign labels, and a missing label never blocks a freeze (D26 blocks on
  unanswered *interview* questions).
- Body `AuthorRequest`: `{ "nl": "str (required)", "persona": "member", "answers": { "question": "answer" } }`.
- Returns `AuthorNeedsAnswers | AuthorFrozen`:
  - Interview open (D26 blocks freeze): `{ "status": "needs_answers", "questions": ["str", …] }`
  - Frozen: `{ "status": "frozen", "test_id": "uuid", "version": 1, "calibration": {…}, "steps": 0, "bundle": {…} }`

### `POST /projects/{project}/tests/{test_id}/run` — one-off run of a single test
- Query: `target_id` (required). Wraps the test in a one-off suite, same async launch path (D32/B1).
  **202** `LaunchAccepted` / **409** target busy.

### `POST /projects/{project}/runs/select` — change-driven selection (preview, optional launch)
- Query: `target_id` (required). Body `SelectRequest`: `{ "change": "NL str (required)", "launch": false }`.
- Returns `SelectResponse`: `{ change, selection: [{test_id, test_key, score, selected, cold_start,
  reason}], selected: [uuid…], run: {suite_run_id, verdict: null} | null }` — launch is async (B1);
  poll `/suite_runs/{id}`. **409** if launch requested on a busy target.

## Runs + evidence

### `GET /projects/{project}/runs` — list runs (A2 projection + A3 controls)
- Query: `limit` (≤500, default 50), `offset`, filters `test_id`, `verdict`, `environment`,
  `pinned`, `trigger`, `since`, `until` (ISO datetimes; AND-combined), and `role`
  (**default `primary`** — run history hides child runs; `any` or `retry|heal|verify` widens it, B3).
- Returns `RunListItem[]` ordered `started_at desc`:
  `{ id, test_id, test_key, test_version, role, verdict, error_class, environment, duration_ms, pinned,
     off_canonical, param_row, browser, viewport, incident_id, trigger, started_at, finished_at }`

### `GET /runs/{run_result_id}` — run detail + steps
- 404 if missing. Returns `RunDetail` — the full `run_result` row plus `test_key`,
  `intent_descriptor`, `intent_nl`, and `steps: StepEvent[]`.

### `GET /runs/{run_result_id}/steps` — step events (poll tier, B1/B2)
- Query: `after` (default 0) — a `StepEvent.id` cursor; returns only rows newer than it.
- Returns `StepEvent[]` (the live D33 trail, ordered by `id`):
  `{ id, step_index, step_id, intent, action, rung_used, status, error_class, budget_ms, elapsed_ms,
     network:[{method,url,status,matched_expectation}], detail:{…, fallback} }`
- **Two rows per step (B2):** `status="running"` at step START (elapsed null, budget set — drive the
  "running for _N_ s" timer/budget bar off it), then the terminal row (`passed|failed|skipped`).
  Pair by `step_index`, last row wins. Rows are only ever inserted, never updated (cursor-safe).
- Live-progress recipe: poll with the last seen `id` until `run_result.verdict` is terminal.

### `GET /runs/{run_result_id}/events` — SSE (B1)
- `text/event-stream`; server tails new `step_event` rows (~250ms). One `step` event per row
  (StepEvent JSON), then one `terminal` event `{verdict, error_class, duration_ms}` and the stream
  closes. Query `after` resumes from a cursor.

### `GET /suite_runs/{suite_run_id}/events` — SSE, suite-level (B1)
- Multiplexes per-test progress: `step` events across all member runs (tagged `run_result_id`,
  `test_id`, `test_key`), then `terminal` with the final `SuiteRunDetail` (incl. `results`).

### `GET /runs/{run_result_id}/artifacts/{kind}` — evidence bytes (A1)
- `kind ∈ trace | video | screenshot | network_log`; unknown kind → 422; missing evidence/run → 404.
- `trace` → `application/zip` (Playwright trace, feed to the trace viewer); `video` → `video/webm`;
  `screenshot` → `image/png`; `network_log` → JSON **synthesized from `step_event.network`**
  (`[{step_index, method, url, status, matched_expectation}]` — the `network_log_path` column is never
  written). This is the ONE evidence serve path (the legacy `/runs/{id}/video` is removed); the MinIO
  swap later returns a presigned redirect, same contract.

### `POST /runs/{run_result_id}/pin` — reviewer pin/unpin (A4)
- Body `PinRequest` `{pinned: bool}` → `PinResponse` `{id, pinned}`; 404 if missing.
  Pinned runs are exempt from the D12 prune.

## Tests + versions (A2)

### `GET /projects/{project}/tests` — catalog → `TestListItem[]`
  `{id, test_key, head_version, quarantined, labels, verified_against}`
- Query: `label` (optional) — only tests carrying it; **`label=` (empty) = the Unlabelled bucket**
  (distinct from omitting the param).
- `verified_against` is the head version's **canonical target key** — what lets a client *show* which
  target a label run will use (the derivation itself is the API's, see §Labels).
### `GET /tests/{test_id}` — → `TestDetail` (+ `project_id`); 404 if missing.
### `PATCH /tests/{test_id}` — catalog metadata; body `TestPatch` `{labels?: string[]}` → `TestDetail`
- `labels` **replaces** the whole set (no add/remove verbs). Normalised on write: trimmed, lowercased,
  de-duplicated, order preserved. **422** on a label failing `^[a-z0-9][a-z0-9_-]{0,31}$` or more than
  16 per test; **404** if the test is missing.
- Relabelling writes a `changelog` row (`kind=labels`) and **never mints a `test_version`** — a label is
  catalog metadata, not behaviour, so it stays out of lineage.
### `GET /tests/{test_id}/versions` — lineage → `TestVersionListItem[]`
  `{version, state, reason, parent_version, incident_id, ruling, triggered_by, created_at}`
### `GET /tests/{test_id}/versions/{version}` — → `TestVersion` (+ `bundle`, `verified_against`,
  **`calibration`** — the D7 falsifiability record); 404 if missing.
### `GET /export/{test_id}/{version}` — → `ExportResponse` `{test_id, version, script}`; 404 if missing.

### Golden reference recording (B4 — test-viewer golden panel)
Per-test "how it should look" video: auto-seeded at freeze from a clean cold replay of the frozen
bundle (`source=auto`), a reviewer upload overrides it (`source=manual`). One per test.
- `GET /tests/{id}/golden` — → `Golden` `{test_id, source, reflects_version, created_at}`; 404 if none.
- `GET /tests/{id}/golden/video` — → `video/webm` bytes; 404 if none.
- `POST /tests/{id}/golden` — multipart `file=` upload (reviewer override → `source=manual`); → `Golden`.
- `DELETE /tests/{id}/golden` — 204; 404 if none.

## Suites + suite runs (A2/A4)

### `GET /projects/{project}/suites` — → `Suite[]` `{id, project_id, name, member_test_ids}`
### `POST /projects/{project}/suites` — create (A4); body `SuiteCreate` `{name, member_test_ids}`.
  Upserts on `(project, name)`. Returns `Suite`.
### `GET /suites/{suite_id}` — → `Suite`; 404 if missing.
### `PATCH /suites/{suite_id}` — partial update (A4); body `SuitePatch` `{name?, member_test_ids?}`
  (omitted = untouched). Returns `Suite`; 404 if missing.
### `GET /suite_runs/{suite_run_id}` — post-hoc suite-run read (A2; B1's poll target)
  → `SuiteRunDetail` `{id, project_id, suite_id, mode, trigger, target_id, state(pending|probing|
  running|done|parked), verdict, park_reason, created_at, results:[{id, test_id, test_key, verdict,
  error_class, duration_ms, started_at, finished_at}]}`; 404 if missing.

## Labels

A label is "a suite you don't have to maintain": suite membership is a hand-kept uuid array, so a test
authored today belongs to nothing until someone edits it. Labels make membership a query. Free-form
strings — the vocabulary is the project's, never in engine code or prompts (rule 2).

### `GET /projects/{project}/labels` — → `LabelCount[]` `{label, test_count}`
Derived by `unnest` — there is no registry table, so a label exists exactly as long as some test carries
it and `test_count` is never stale.

### `POST /projects/{project}/labels/{label}/runs` — run every test carrying a label **(async, B1)**
- Query: `target_id` (**optional** — derived when omitted), `trigger` (default `api`).
- **202** `LaunchAccepted {suite_run_id}`; watch it exactly like a suite run.
- Membership = every test carrying the label at head version, **excluding quarantined** ones (the console
  still lists them, marked). Reuses the ONE launch path: a `label:<name>` suite (upserted on
  `(project, name)`, so repeated runs reuse one row) + the same async launch. Each run's `run_task` rows
  still freeze that run's real membership — the suite is the query, the run is the answer.
- **404** if no test carries the label — *before* any `suite_run` exists, so a typo never becomes a
  parked run to decode. (The saved-suite `EMPTY_SUITE` park is unchanged.)
- **Target derivation.** Every test records its canonical environment as
  `test_version.verified_against`, so the API resolves the target instead of asking; the rule lives here
  and not in each client (D32 — the console and `anneal run --label` cannot disagree). One distinct
  target → launch there. Several (or a key matching no target row) → **409**
  `{error: "label spans targets", label, split: [{target_key, target_id, test_count}]}`, a refusal
  rather than a guess: whichever target were chosen, the rest would run read-only, unable to heal or
  re-author. An explicit `target_id` always wins.
- **409** `{error: "target busy", active_suite_run_id}` — the usual launch lock.

## Incidents (§9, D37)

### `GET /projects/{project}/incidents` — inbox list (A3 controls)
- Query: `state`, `klass`, `ruling`, `since`, `until`, `limit` (≤500, default 50), `offset`.
- Returns `Incident[]` ordered `created_at desc` (fields below).

### `GET /incidents/{incident_id}` — detail
- 404 if missing. `IncidentDetail` = incident + `members: IncidentMember[]`
  `{test_id, red_run_id, verify_run_id, applied_version, ruling, test_key, head_version, quarantined}`
  + `changelog`. The money-shot shows red-run **and** verify-run evidence — fetch each via
  `GET /runs/{red_run_id|verify_run_id}/artifacts/{kind}` (B3; verify runs are `role=verify`).

### `POST /incidents/{incident_id}/ruling` — rule an incident **(web-only by convention)**
- Body `RulingRequest` — **exactly one shape** (422 otherwise):
  - Uniform: `{ "ruling": "feature|bug", "ruling_note": "…", "ruled_by": "web" }` — one decision for
    every member (the "Rule all Feature" bulk action).
  - **Per-member (A5, money-shot Direction B):** `{ "members": { "<test_id>": "feature|bug", … },
    "ruling_note": "…" }` — one ATOMIC POST; the incident-level ruling is **derived**
    (`feature|bug|mixed`); each decision recorded on `incident_member.ruling`.
- 404 if missing; 409 if already ruled/closed or a given test_id is not a member.
- Returns `RulingResponse`: `{ ruling: "feature|bug|mixed", affected: [test_key…], ruling_note,
  members: {test_id: ruling} | null }`.
- feature → that member's provisional becomes canonical; bug → roll back + file a bug (C5).

## Reads (A2/A3)

### `GET /projects/{project}/changelog` — query `test_id`, `kind`, `since`, `limit` (≤500, default 100),
  `offset` → `ChangelogEntry[]` `{id, project_id, test_id, incident_id, kind, summary, ruling_note, created_at}`
### `GET /projects/{project}/targets` — → `Target[]` `{id, project_id, key, base_url, timezone,
  date_format, max_parallel, profile}`
### `GET /targets/{target_id}/territory` — D25 TerritoryNotes (latest version) → `TerritoryNotes`
  `{target_id, version, notes, stale, authored_by, created_at}`; 404 if none.
### `GET /projects/{project}/sessions` — session health → `SessionHealth[]` `{credential_id, persona,
  state(valid|expired|unknown), seeded_at, last_validated}`. The storage-state path stays server-side.
  Re-seed/validate triggers are B5.

---

## Incident fields (list + detail)
`{ id, project_id, state(open|propagating|ruled|closed), klass(locator|behavior|env), ck_surface, ck_step_sig,
   ck_error_class, representative_test_id, change_brief, rep_concluded, ruling(feature|bug|mixed),
   ruling_note, ruled_by, ruled_at, budget_agent_runs, budget_llm_calls, cap_agent_runs, cap_llm_calls,
   created_at, closed_at }`
cluster_key is the three `ck_*` columns; budget is the `budget_*`/`cap_*` pairs (the UI composes `{spent, cap}`).

## CLI — `anneal <cmd>` (thin httpx client; `ANNEAL_API`, default `http://localhost:8080`)
| Command | Calls | Exit |
|---|---|---|
| `run <project> <suite> --target` | `POST …/suites/{suite}/runs` then **polls `GET /suite_runs/{id}` to terminal** (B1) | 0 if verdict ∈ {green, green_with_heals, provisional}, else 1 (incl. 409 busy) |
| `run <project> --label L [--target]` | `POST …/labels/{L}/runs`, same poll. `--target` optional (derived); prints the resolved target | as above; 1 on 404 no-such-label and on the 409 spans-targets refusal (which prints the split) |
| `status <run_result_id>` | `GET /runs/{run}` | 0 |
| `export <test> <version>` | `GET /export/{test}/{version}` | 0 |
| `author <project> <test_key> --target --nl [--answer q=a]…` | `POST …/tests` | 2 if interview open |
| `select <project> --target --change [--launch]` | `POST …/runs/select` | — |
No CLI command for rulings (deliberate).

## Evidence / artifacts
`LocalDiskStore` (D34) writes trace/video/screenshots under `ANNEAL_ARTIFACTS` (default
`./.anneal/artifacts`), one dir per run; `run_result.*_path` are absolute local strings. **Bytes are
served by `GET /runs/{id}/artifacts/{kind}`** (A1). Retry/heal/verify replays are now **real
run_results** (`role ∈ retry|heal|verify`, B3) with their own artifact dirs + step trails — addressable
like any run; `incident_member.verify_run_id` links the verify replay that proved a provisional.

## Auth / tenancy
**None** (decided open for this cycle — BACKEND-SPEC §Auth; do not bind beyond localhost). Org is
implicit/single (D17); `project.id` is the top key. The `credential`/`session` tables model the **target
app's** logins, not Anneal operators. `ruled_by` is free-text provenance, not a principal.
