# BUILD-SPEC.md — build the Anneal console UI

> **You are a coding agent building the production console.** This is your entry point. The prototypes in
> this folder are the **visual + interaction source of truth**; this doc tells you how to turn them into a
> real UI wired to the Anneal API, and what is *not* yet backed by an endpoint so you don't build against
> air. Read the prototypes by rendering them locally (`cd design && python3 -m http.server 8777`),
> not by guessing from this text.

## 0. Read in this order
1. **`design/README.md`** — what each prototype is + the decisions behind them.
2. **The prototype `.html` files** (§3 table) — open each; the markup, `tokens.css` values, and the mock
   JS at the bottom are the spec. Copy layout/interaction from here.
3. **`design/tokens.css`** — the canonical design system (import this; don't re-inline).
4. **`../API.md`** — the data contract (shapes you render). **`../UI-READINESS.md`** — buildable-now vs
   target-state (authoritative on what's stubbed). **`../DESIGN-BRIEF.md`** — the product meaning of every term.
5. **`design/BACKEND-SPEC.md`** — to *build* (not just mock) the missing endpoints: streaming, artifact
   serving, typed contracts, the read-holes. Each `.stub` in the UI maps to one item there. Build UI and
   backend together, screen by screen. **The 2026-07-15 interview decisions are folded into its items
   (Wave A = additive API, ships first; Wave B = engine surgery) — its items are authoritative and resolve
   every ⚠/ambiguity in this doc (stack, ruling shape, CORS, undesigned screens, suite CRUD).**

## 1. Stack
**Decided (2026-07-15): React + TypeScript (Vite)**, client types generated from `/openapi.json`
(openapi-typescript). In prod the FastAPI app serves the built frontend (same origin); dev uses a CORS
allowlist env var. What to preserve:
- **Theme via `tokens.css` custom properties.** Import it once; style through the variables. Light is the
  default; dark is a manual toggle that sets `<html data-mode="dark">` (see tokens.css header). Do **not**
  wire `prefers-color-scheme`.
- **The prototype markup is the layout reference.** Its structure + class names map straight onto
  components. The mock JS (data arrays + render functions at the bottom of each file) shows the exact
  interaction; **replace it with API calls** (§4).
- **No webfonts, no external assets** — the aesthetic is a monospace-forward system stack by design.

## 2. Locked design decisions (do not re-litigate)
- **Incident ruling = Direction B, per-member.** Each affected test is accepted (✓ feature) or rejected
  (✕ bug) individually, plus a "Rule all remaining Feature" bulk action. (A/C/D directions were dropped.)
- **Click-driven. No keyboard shortcuts.** (Explicitly removed.)
- **Light default + manual dark toggle** (§1).
- **Monospace-forward** type; **colorblind-safe verdicts** = colour + glyph + label (see tokens.css). Never
  encode a verdict/ruling in hue alone.
- **`provisional` reads calm** (pass-family, teal half-ring ◑) — it is "green, accepted for now, awaiting
  batch review," NOT a warning. Never style it amber/red. (DESIGN-BRIEF §4.)
- **Under-test screenshots render dark** inside a browser-frame chrome — that's a picture of the target
  app, independent of console theme.

## 3. The screens (drill path: Suite → Test → Run → Incident)

| Screen | Prototype | Surface | Purpose |
|---|---|---|---|
| Author | `design/author.html` | 1 | Author from NL → D26 interview → freeze-review → calibrated frozen test |
| Suite & run | `design/suite.html` | 2 | Run a suite / change-driven selection against a target; **create/edit suite membership** (modal) |
| Runs list | `design/runs-list.html` | 3 | Top-level table of every run (verdict/env/trigger/duration/pinned); row → run inspector |
| Test viewer | `design/test-viewer.html` | — | One test: intent, flow, oracle, calibration, version lineage, run history |
| Run inspector | `design/run-inspector.html` | 3 | One run: steps → atomic actions, before/after frames, replay, network/console |
| Incident money-shot | `design/money-shot.html` | 4 | Rule a drift feature-vs-bug (per-member) |
| Territory | `design/territory.html` | — | What the engine learned about a target (D25 TerritoryNotes): routes, widget idioms, flows, terms |
| Sessions | `design/sessions.html` | — | Seeded target-app logins per persona/target: state, last-validated, seeded, re-seed trigger |
| Changelog | `design/changelog.html` | — | Project-wide append-only feed: heals, re-authors, rulings, authoring — links to test + incident |

All ten prototypes now exist. The four supporting screens (Runs list, Territory, Sessions, Changelog) and
suite create/edit reuse the shell + primitives from the first five — no new design language; every
not-yet-backed element is wrapped in a `.stub` marker mapping to a `BACKEND-SPEC.md` item.

## 4. Per-screen data contract (endpoints are real unless marked ⚠ needs-endpoint)

All under `POST/GET /projects/{project}/…`. Base = the ONE FastAPI app (`packages/anneal-api`).

### author.html
- **Author from NL** — `POST /projects/{project}/tests?test_key=&target_id=` body `{nl, persona, answers?}`.
  Returns **either** `{status:"needs_answers", questions:[…]}` (the D26 interview — ambiguities that would
  change what the test *asserts*; unanswered = blocked, **never guessed**) **or** a frozen, calibrated
  `test_version`. The flow is: POST → if `needs_answers`, render the questions, collect answers, **re-POST**
  with `answers` merged (blank answers don't count — `anneal_authoring/service.py`).
- **On freeze** — the bundle is D7-**calibrated** (proven falsifiable) and **TerritoryNotes** (D25) are
  written from recon. Surface both as the "why you can trust this" moment.
- ⚠ **Freeze-review** (inspect the proposed network expectation / delete it / scope the assert *before*
  freezing, HLD §5/§7) is **target-state** — today it freezes one-shot. ⚠ No live view of the hot lane
  authoring (synchronous).

### suite.html
- **Members list** — from the suite's `member_test_ids`. ⚠ **no list/get-suite route yet** (suites are
  ad-hoc; `create_suite` exists). Needs `GET /suites` + `GET /suites/{id}`.
- **Change-driven selection** — `POST /projects/{project}/runs/select?target_id=` body `{change, launch}`
  → `{selection:[{test_id, test_key, score, selected, cold_start, reason}], selected:[…], run?}`. Scoring is
  deterministic (`anneal_classify/selection.py`): `0.6·semantic + 0.4·structural`, threshold **0.45**,
  cold-start = unmapped test. Render `score`/`selected`/`cold_start`/`reason` verbatim — the reason strings
  are authored server-side.
- **Launch** — `POST /projects/{project}/suites/{suite_id}/runs?target_id=&trigger=manual|api|deploy_hook`
  → `SuiteRunReport {suite_run_id, state: done|parked, verdict, park_reason, results:[{run_result_id,
  test_id, verdict, error_class, duration_ms}]}`. Suite verdict precedence (D29, `scheduler.py`):
  `red > parked_auth > parked_env > provisional > green_with_heals > green`. Park reasons:
  `AUTH_EXPIRED · ENV_CAPACITY · ENV_INFRA · EMPTY_SUITE`.
- ⚠ **Live ticking is stubbed** — launch is synchronous; there's no stream. Show the running state, but
  today you get the whole report at once. ⚠ **Re-seed session** (for `parked_auth`) has no endpoint.

### test-viewer.html
- **Test + head bundle** — `test` row (`test_key, head_version, quarantined`) + head `test_version.bundle`
  (`intent, policy, steps`, postconditions = the oracle). ⚠ **no test-catalog/get-test route** — add
  `GET /tests` + `GET /tests/{id}`.
- **Version lineage** — `repo.list_test_versions` / `get_test_version` exist (fields: `version, state,
  reason, parent_version, ruling, incident_id, triggered_by`) but ⚠ **have no route**. Add them.
- **Calibration (D7)** — `test_version.calibration` (`negative_control`, network `discriminativeness`).
  ⚠ **not returned by any read endpoint** — expose it on the get-test-version route.
- **Run history** — `GET /projects/{project}/runs` returns `{verdict, error_class, environment,
  duration_ms, pinned, started_at, test_id, test_key}`. ⚠ currently `limit`-only + no `test_id` filter —
  filter client-side for now, or add `?test_id=`. `off_canonical` is on the run row but not in the list
  projection — add it.
- **Changelog** — `GET /projects/{project}/changelog` (`kind, summary, ruling_note, created_at`). ⚠ filter
  by `test_id` not yet a param.
- **Export** — `GET /export/{test_id}/{version}` → standalone Playwright script (real).

### run-inspector.html
- **Run + steps** — `GET /runs/{run_result_id}` (+ `intent_descriptor`, `intent_nl`) then
  `GET /runs/{run_result_id}/steps` → `step_event` rows: `{step_index, step_id, intent, action, rung_used,
  status, error_class, budget_ms, elapsed_ms, network, detail}`. NL step → atomic actions maps onto the
  step tree; `rung_used` drives the rung-ladder detail; `network` (jsonb) feeds the Network tab.
- ⚠ **Console tab** — there is no `console` column on `step_event` today; treat it as target-state (or
  derive from `detail`). ⚠ **Evidence bytes** (`trace_path/video_path/screenshot_path/network_log_path`)
  are server-local **path strings** — no file-serving endpoint. Render presence (`✓ / —`) and the
  before/after **frames** from `step_event.detail`; the browser-framed screenshot is a placeholder until
  artifact-serving lands. ⚠ **Live step-ticking / replay** is stubbed (synchronous launch).

### money-shot.html
- **Incident detail** — `GET /incidents/{incident_id}` → incident (`state, klass, ck_*, change_brief,
  budget_*`, …) + `members` (`incident_member`: `test_id, red_run_id, applied_version`) + `changelog`.
- **Per-member diff** — compose per member: fetch that member's red run + verify run steps
  (`GET /runs/{id}/steps`) and diff their step intents / network. (No single "diff" endpoint.)
- **Ruling** — `POST /incidents/{incident_id}/ruling` body `{ruling: "feature"|"bug", ruling_note,
  ruled_by}`. **feature** → provisional versions become canonical; **bug** → roll back + file a bug.
  Web-only by convention. ⚠ The prototype's **per-member** ✓/✕ is the chosen UX; **decided:** one atomic
  POST with `{members: {test_id: "feature"|"bug"}, ruling_note}` — collect ✓/✕ client-side, submit once
  (incident-level ruling becomes derived `feature|bug|mixed`). See BACKEND-SPEC §Decisions.
- ⚠ Evidence bytes stubbed (same as run inspector).

## 5. What's stubbed / target-state (don't wire; mock + mark with `.stub`)
Authoritative list in `../UI-READINESS.md`. Summary: **no live progress/streaming**, **no artifact
serving** (evidence = path strings), **no auth**, and read-holes with **no route yet**: suite list/get,
test catalog + version lineage, calibration read, `GET /suite_runs/{id}`, TerritoryNotes, session health +
re-seed, target list/profile. List controls are minimal (`runs` limit-only; `incidents` state-only). Every
place the prototypes depend on one of these is already wrapped in a `.stub` marker — keep them.
**To build these for real (not just mock), follow `design/BACKEND-SPEC.md`** — each `.stub` maps to one
item there, in priority order.

## 6. Recommended sequence
1. **Typed API contract first.** The API bodies are untyped `dict`s today. Add Pydantic request/response
   models + OpenAPI and generate a typed client — do this before building screens (UI-READINESS §recommended).
2. **Import `tokens.css`; build the shared primitives** (verdict pill, chip, btn, app shell, browser-frame,
   tab bar) once as components.
3. **Build screens in drill order** using each prototype as the reference: Runs list (design first) →
   Run inspector → Suite → Test viewer → Incident money-shot → Author (D26 interview).
4. **Wire the real endpoints** from §4; mock only the §5 stubs, keeping the `.stub` markers so it's obvious
   what's pending backend work.
5. **Close the read-holes** (additive GETs) alongside the screens that need them.

## 7. How to consume the prototypes
Each `.html` file = inlined `tokens.css` + screen CSS + semantic markup + a `<script>` with a mock data
array and render functions. To port: (a) lift the markup into components, (b) import `tokens.css` instead
of the inlined `:root`, (c) delete the mock arrays and call the §4 endpoints, (d) keep the `.stub` markers
on anything in §5. The mock data is realistic (incident `INC-4192`) so you can diff your wired output against the
prototype.
