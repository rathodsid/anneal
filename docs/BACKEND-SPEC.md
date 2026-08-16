# BACKEND-SPEC.md — close the gaps so the UI is real

> Companion to `BUILD-SPEC.md`. That doc builds the UI against today's API and marks everything not yet
> backed by an endpoint with `.stub`. **This doc is the work that removes those stubs.** Every `.stub`
> marker in the prototypes maps to exactly one item below — when the item ships, delete the stub and wire
> the real thing. Authoritative gap list + severity: **`../UI-READINESS.md`**.
>
> All 2026-07-15 interview decisions are **folded into the items below** — the items are authoritative and
> self-consistent; there is no separate override list. A provenance-only decision log is at the bottom.
> Parity is green; the build starts now, **Wave A first**.

## Ground truth (verified in code, 2026-07-15)
- API is a **monolithic `packages/anneal-api/src/anneal_api/app.py`** — **14 handlers** (including
  `GET /runs/{id}/video`, already a `FileResponse`, and `POST /projects/{p}/tests/{test_id}/run`), untyped
  `dict` bodies, no routers, no OpenAPI models. `../API.md` lags the code (says 12, "no file serving") —
  **trust the code**; A0 regenerates `API.md` from the typed contract.
- **No streaming, no async launch, no auth, no CORS, no `Depends`.** `POST …/suites/{id}/runs` `await`s
  `run_suite` and returns only when the whole suite is done.
- **Sync-in-async hazard:** the DB engine is **sync SQLAlchemy** (`anneal_db/engine.py`) and the LLM client
  is **sync `anthropic.Anthropic(timeout=90)`** (`anneal_llm/client.py`). `run_suite` must never share the
  API's event loop — a heal/classify LLM call would freeze the live-progress endpoints for up to 90s.
- **Key enabler:** `step_event` rows are written **live during the run** (`anneal_db/sink.py` `emit`). The
  table already has a **`bigserial id`** (monotonic — the natural poll/SSE cursor) and
  **`created_at timestamptz`** (timers need no new column).
- **D33 invariant (touch with care):** `_ensure_step_events_visible` + `_finalize_with_evidence`
  (`scheduler.py`) gate every PASS verdict on `count_step_events(run) >= steps_run`, and
  `count_step_events` counts **all** rows unfiltered. Anything that changes row cardinality (B2) must
  change this counting **in the same PR** or verdicts finalize over incomplete trails.
- **Artifacts:** `run_result.{trace,video,screenshot}_path` are absolute local paths written by
  `anneal_artifacts.store.LocalDiskStore` (`ArtifactStore` Protocol; MinIO is the later swap → presigned
  URL, same handler). **`network_log_path` is never written by anything**; per-step network lives in
  `step_event.network` (jsonb).
- **Verify/retry/heal runs are not addressable:** they use literal ids (`"reauthor-verify"` is a **fixed
  artifact dir overwritten by every incident**), `_CollectSink` (step events discarded), and create **no
  `run_result` rows**. `incident_member` links `red_run_id` only — no verify-run linkage.
- **Authoring records nothing:** `anneal_authoring/service.py` opens a bare browser context — no
  `recording(...)`, no trace/video. The golden auto-seed (B4) must first add recording to authoring.
- Several read-holes already have **repository functions with no route** (e.g. `list_test_versions`,
  `get_test_version`) — near-zero-cost to expose.

---

## Wave A — additive API layer (zero engine touch; ships first, fast)

Wave A alone unblocks most of the UI. Nothing in it modifies the run/authoring engine or the parity
fixtures. Do not let Wave B block it.

### A0 — Typed contracts + routers + frontend boundary  ✅ *shipped 2026-07-16*
- **Pydantic request/response models** for every endpoint; replace the untyped `dict = Body(...)` handlers.
  Regenerate **`../API.md`** from the typed contract (it is stale at HEAD).
- Split `app.py` into **`APIRouter`s**: `runs`, `suites`, `tests`, `incidents`, `changelog`, `export`,
  `sessions`, `targets`.
- **OpenAPI on**; generate the frontend client from `/openapi.json`. Stack (decided): **React + TypeScript
  (Vite)**, types via openapi-typescript — the UI never hand-writes fetch shapes.
- **Frontend boundary (decided):** prod = FastAPI serves the built frontend (same origin, one deployable,
  replaces the `CONSOLE_HTML` page); dev = **CORS allowlist via env var** for the dev-server origin. CORS
  is required and was previously specified nowhere.
- **Remove the legacy `GET /runs/{id}/video`** — no back-compat (decided); A1 is the only serve path.
- **Acceptance:** `/openapi.json` describes all endpoints; the frontend imports the generated client;
  `API.md` regenerated.

### A1 — Artifact serving  ✅ *shipped 2026-07-16* *(network_log synthesized on demand)*
- `GET /runs/{run_result_id}/artifacts/{kind}`, `kind ∈ {trace, video, screenshot, network_log}` →
  `FileResponse` of the stored `*_path` (validate `kind`, guard path traversal, 404 if missing).
  Playwright trace is a `.zip` — serve it for the trace viewer; frames come from `step_event.detail`.
- **`network_log` is synthesized on demand** from `step_event.network` rows (JSON response) — nothing
  writes `network_log_path`, and this needs **no run-path change**. (Persisting a file at run-finish is an
  optional Wave-B tweak if a downloadable artifact is later wanted.)
- Keep it behind the `ArtifactStore` Protocol so the MinIO swap is a presigned-URL redirect, no handler change.
- **Removes stubs in:** `run-inspector.html` and `money-shot.html` **red-run** evidence. Verify-run
  evidence additionally needs **B3**.

### A2 — Read-holes  ✅ *shipped 2026-07-16*
| Gap | Exists in repo | Add route | Unblocks (screen) |
|---|---|---|---|
| Test catalog | (needs a list query) | `GET /projects/{p}/tests`, `GET /tests/{id}` | test-viewer, suite members |
| **Version lineage** | `list_test_versions`, `get_test_version` ✅ | `GET /tests/{id}/versions`, `…/versions/{v}` | test-viewer › Versions |
| **Calibration** | `test_version.calibration` column ✅ | include in the version response | test-viewer › Overview (D7 card) |
| Suite get/list | `create_suite` only | `GET /suites`, `GET /suites/{id}` | suite.html members |
| **Suite-run post-hoc** | (none) | `GET /suite_runs/{id}` (`state/verdict/park_reason/mode/trigger/created_at/results`) | suite last-run, re-inspect — **and the poll target B1's CLI + console depend on: must ship before B1** |
| TerritoryNotes | `upsert_territory_notes` (write only) | add a repo getter + `GET /targets/{id}/territory` | Territory view, author.html |
| Session health | `session.{state,last_validated,seeded_at}` (unread) | `GET /projects/{p}/sessions` | Sessions view, suite `parked_auth` |
| Target list/profile | `get_target` only | `GET /projects/{p}/targets` | suite target selector |
| Run detail extras | `off_canonical`/`param_row`/`browser`/`viewport`/`test_version`/`incident_id` on the row; **`trigger` needs a `run_task → suite_run` join** | add to `list_runs` projection (the Runs-list columns: ver, trigger, incident chip, off-canonical tag) | Runs list, test-viewer › Run history |

### A3 — List controls  ✅ *shipped 2026-07-16*
- `GET …/runs`: `offset`/cursor + filters `verdict|environment|test_id|pinned|trigger|date` (+ `role`, see B3).
- `GET …/incidents`: pagination + `klass|ruling|date` (today `state`-only).
- `GET …/changelog`: `test_id|kind` filters, pagination, `since`.
(Filter set confirmed against runs-list.html / changelog.html mock data, 2026-07-16.)

### A4 — Suite CRUD + pin  ✅ *shipped 2026-07-16*
- `POST /suites` and `PATCH /suites/{id}` (`name`, `member_test_ids`); GET routes are in A2.
  (`repo.create_suite` exists — mostly additive.)
- **Pin/unpin:** `run_result.pinned` column exists → `POST /runs/{id}/pin` `{pinned}`.

### A5 — Per-member incident ruling  ✅ *shipped 2026-07-16* *(uniform `ruling` still accepted for the bulk action)*
- **Decided shape:** **one atomic** `POST /incidents/{id}/ruling` with
  `{members: {test_id: "feature"|"bug"}, ruling_note}` — the UI collects ✓/✕ client-side and submits once.
- Add `incident_member.ruling`; the incident-level `ruling` becomes **derived** (`feature|bug|mixed`);
  changelog entries per member. One open→ruled transition — no "partially ruled" state.

---

## Wave B — engine surgery (each item behind `./verify.sh`; see the fixture note)

One B-item per PR; `./verify.sh all` green before the next, plus `ANNEAL_LIVE=1 ./verify.sh parity-live`
after B1–B3 (the golden parity corpus is RETIRED per the 2026-07-13 pivot; parity-live is the corpus gate).
**Frozen-bar rule (verify.sh):** `baseline.json`, `schemas/*.json`, and the pinned fixture are hash-guarded —
the agent may NEVER modify or re-pin them. B2/B3 change event/run_result cardinality; if any m-gate or bar
file must change to accommodate that, **stop and hand it to the human**, who diffs and re-pins deliberately.
Schema changes (role column, `verify_run_id`, member ruling, partial unique index) ship as **alembic
migrations** (`migrations/`), never raw DDL edits.

### B1 — Async launch + live progress  ✅ *shipped 2026-07-16*
- **Execution model (decided):** launch creates the suite_run row and spawns a **dedicated thread running
  its own asyncio loop** for `run_suite` — never FastAPI `BackgroundTasks` (the sync DB/LLM calls would
  stall the very endpoints users watch). Returns `{suite_run_id}` immediately; same for
  `POST …/runs/select?launch=true`.
- **One active suite_run per target — mechanism, not just policy:** a **partial unique index** on
  `suite_run(target_id) where state in ('pending','probing','running')` makes the insert itself the lock
  (no check-then-insert TOCTOU). Conflict → **409** with the active `suite_run_id`. Cleared by terminal
  states and by the reaper.
- **Startup reaper:** on API boot, terminal-ize stale `pending/probing/running` suite_runs (and their
  unfinished run_results) with park_reason **`INTERRUPTED`** — a **new enum value**, deliberately not
  `ENV_INFRA` (the env wasn't down; the server bounced). The UI renders it distinctly.
- **Progress, two tiers (poll first, SSE second):**
  - **Poll:** `GET /runs/{id}/steps?after={event_id}` — the cursor is **`step_event.id` (bigserial)**, not
    `step_index` (survives B2's two-row-per-step shape). Client polls until `run_result.verdict` terminal.
  - **SSE:** `GET /runs/{id}/events` (`StreamingResponse` tailing new rows by `id`, ~250ms) +
    `GET /suite_runs/{id}/events` multiplexing per-test progress.
- **CLI (decided: expendable, no back-compat):** rewrite `anneal run` to POST then poll
  `GET /suite_runs/{id}` to terminal — the blocking UX and CI exit-code gate are preserved from the outside.
- **Removes stubs in:** `run-inspector.html` (live ticking/replay), `suite.html` (live suite-run),
  `author.html` (hot-lane live view — after B6's flow exists).

### B2 — Step-started events  ✅ *shipped 2026-07-16* *(optional-sink duck-type: collectors unaffected)*
- **Mechanism (resolved):** **two rows per step** — insert a `status='running'` row at step start; insert
  the terminal row at completion. No in-place updates (a mutated row is invisible to the id-cursor poll and
  the SSE tail).
- **Same PR:** `count_step_events` counts **terminal rows only** (`status != 'running'`), so
  `_ensure_step_events_visible` / `_finalize_with_evidence` keep their exact semantics. Without this, PASS
  verdicts finalize over incomplete trails.
- `created_at` (exists) gives "running for _N_ s"; `budget_ms` gives budget bars.

### B3 — Verify/retry/heal runs become real `run_result`s  ✅ *shipped 2026-07-16*
- Add a **role** column: `primary|retry|heal|verify`. Real ids, **unique artifact dirs** (fixes the
  `"reauthor-verify"` overwrite bug), real `DBStepEventSink` (step trails for diffing).
- Add **`incident_member.verify_run_id`** — without the linkage the money-shot cannot find the verify run
  it needs to promote (`red_run_id` alone doesn't reach it).
- `list_runs` defaults to `role=primary` (run history stays clean); `?role=` filter exposes the rest.

### B4 — Golden reference recording  ✅ *shipped 2026-07-16* *(auto-seed = clean freeze replay, per 2026-07-16 decision; manual upload overrides)*
- **Precondition (verified missing, now budgeted):** add `recording(...)` to the authoring browser context
  in `anneal_authoring/service.py` — today authoring stores **no** artifacts, so there is nothing to seed from.
- At freeze, the authoring recording becomes the per-test **auto-golden**; `POST` upload/replace/remove
  lets a reviewer override; served via the A1 path (test-scoped artifact).

### B5 — Session re-seed/validate triggers + re-baseline  ⛔ **CUT this cycle (2026-07-16 decision)**
- re-seed/validate: owned by the auth workstream (`design/MULTI-PLATFORM-AUTH.md`) — never this lane.
- re-baseline: **cut** — the semantics are ambiguous (golden re-record vs canonical re-freeze vs repoint
  target; only changelog.html shows it, as a golden re-record) and no concrete product need is pinned yet.
  The `↻ Re-baseline` button (test-viewer.html) stays a `.stub`. Revisit when the need is concrete; if it
  turns out to be "golden re-record," it's a thin reuse of B4's `record_golden` against the current head.

### B6 — Freeze-review  ⛔ **CUT this cycle (2026-07-16 decision)**
- Turns `author_and_freeze`'s one-shot POST into a **stateful two-phase flow** (author → un-frozen draft →
  human inspects/deletes/scopes → resume → calibrate → freeze). Largest Wave-B item; reshapes the
  just-stabilized authoring path. Deferred — the `.stub`s in author.html stay as target-state.

---

## Auth (decided: stays open this cycle)
No token, no user model — single-operator; D17 revisited later. ⚠ **Understand the exposure after B1:**
an open port means anyone reachable can fire unbounded Chromium+LLM jobs (real token spend, resource DoS,
fire-and-forget) and hit destructive endpoints (re-baseline). **Do not bind beyond localhost.** If this
ever reaches even a shared internal network, the designated first mitigation is a single shared-secret
header on mutating + launch endpoints (ten lines) — cheap insurance, no user model needed.

## How this pairs with the UI build
Build UI and backend **together, screen by screen**: the frontend agent builds each screen against the
typed client (A0), mocking only what's still stubbed; as each item lands (A1→B6) it deletes the matching
`.stub` and wires the real endpoint. The `.stub` markers in the prototypes are the shared checklist.
Wave A unblocks: Runs list, test-viewer, suite management, incident inbox + per-member ruling, red-run
evidence. Wave B adds: live runs, verify evidence, golden, re-seed/re-baseline, freeze-review.

## Decision log (2026-07-15 interview — provenance only; the items above are authoritative)
Exec = dedicated thread + own loop · busy target = 409 · crash = startup reaper (`INTERRUPTED`) ·
CLI = expendable, polls to terminal · live grain = step-started events (two-row) · verify/retry/heal =
real run_results + `verify_run_id` · legacy `/video` removed, `network_log` synthesized · golden = auto-seed
+ manual override · ruling = one atomic per-member POST, derived `mixed` · item-5 actions all in
(re-seed, re-baseline, freeze-review as B6) · auth = open this cycle · FE = React+TS, served by FastAPI in
prod, dev CORS env var · undesigned screens (Runs list, Sessions, Territory, Changelog) all in ·
full suite CRUD · parity green, start now, Wave A first.
