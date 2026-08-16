# Anneal — Product Requirements

**Scope:** target-agnostic web QA engine.
**Companion docs:** `HLD.md` (design), `LLD.md` (implementation contracts), `API.md` (backend surface, as-built), `UI-READINESS.md` (UI gap audit).

> **Thesis:** An agent authors and re-authors; **code replays**; every UI change becomes either an auto-applied locator diff or a reviewed behavioral diff — and nothing changes silently.

This document is the product view: who Anneal is for, what it does today, where it is honestly incomplete, and the requirements and sequencing to turn it into a deployable, multi-user product. Capability claims here are grounded in the code at HEAD, not aspiration.

---

## 1. Problem & vision

Automated web QA forces a bad trade. **Frozen scripts** (Playwright/Selenium suites) run fast and deterministically but rot the moment the UI changes — a renamed selector reds the suite, and someone hand-repairs it. **Live AI agents** absorb UI change silently but are slow, non-deterministic, and will happily "pass" a flow that is actually broken, hiding real regressions behind a confident green.

Anneal's wager is that you don't have to choose. An agent does the expensive, intelligent work **once** — authoring a test from a plain-English description by driving the real app — and freezes it into a deterministic artifact that replays **cold, in seconds, with zero LLM**. When the app changes, the engine either **heals** a moved selector automatically (and records the diff) or **re-authors** a changed behavior and surfaces it as one reviewable incident. The suite keeps itself current; it never changes silently.

**Who it's for:**
- **QA / product engineers** who want to describe a flow in plain English and get a fast, reliable, self-maintaining regression test — without hand-writing or hand-repairing selectors.
- **A reviewer** (today, one person) who rules on behavioral changes: is this drift a feature (accept) or a bug (reject + file)? One ruling propagates to every affected test.
- **CI / release automation** that triggers suites on deploy and gets a finite, honest verdict (`green · green_with_heals · provisional · red · parked`).

---

## 2. What Anneal does today

The **engine is built and works.** Eleven packages, three execution lanes (HOT authoring · WARM healing · COLD replay), one canonical Test Bundle artifact. See `HLD.md`/`LLD.md` for the architecture.

**Capabilities that work today:**

- **Author a test from natural language.** The hot lane recons the app, drives it agentically, resolves each step to a locator ladder + postcondition + (for mutating steps) a network expectation, runs a negative-control calibration so the assertions can actually fail, and freezes a deterministic Bundle. An ambiguity interview blocks the freeze on questions that would change what the test asserts.
- **Replay cold, fast, deterministically.** A frozen Bundle compiles to a pure-Playwright step plan and runs with no LLM in the loop — seconds, not minutes — with trace, video, network log, and per-step events captured on every run.
- **Heal locator drift automatically.** A moved/renamed selector re-resolves via the frozen fallback ladder; the heal is verified red→green, auto-applied as a new version, and written to the changelog. Multiple drifts drain in one session.
- **Re-author behavioral change into one reviewable incident.** When a flow genuinely changed, the engine re-authors it, marks it provisional (suite stays green), and clusters affected tests under a single incident with a change brief; one ruling propagates to all members or rolls them back.
- **Honest oracle.** A never-created / unauthenticated negative control must go red at freeze time — a test that can't fail is rejected.
- **Change-driven selection.** An NL change description picks the run set from the observed surface inventory, with a human-readable reason per test.
- **Two thin clients over one API:** a single-page **console v0** (author + run + list runs + run detail) and a **CLI** (`run · status · export · author · select`). `anneal export` emits a standalone Playwright script for any frozen test.

**Verified status:**
- **Golden mechanics gate** (`verify.sh`, M0–M4 on a hermetic app): **green** — author→freeze→cold-replay, drift→red→heal→re-freeze, honest-red, determinism, no-residue.
- **Live target (real product) parity:** **partial.** Login+list, agent-create, and the honest-red control pass; crew-create passes (self-healed a live drift); campaign-from-NL authors and runs but leaves residue; the full campaign CRUD delete-leg is not yet green. Live authoring + cold replay against the real app is demonstrable.

---

## 3. What it is **not** yet

Anneal today is a **single-operator, locally-run engine with a working API and a bare-bones console.** It is not yet a product you can deploy for a team. The gaps are structural, not cosmetic, and are documented in `UI-READINESS.md` / `API.md`:

- **No authentication or operator identity.** Every API endpoint is open; there is no login, no users, no roles, no per-tenant separation (org is implicit/single). `ruled_by` is free text, not an authenticated principal. Not safe to expose to multiple people.
- **Synchronous execution, no live progress.** Launching a run blocks the HTTP request until the entire suite finishes; there is no streaming (SSE/WebSocket) or polling anywhere. You cannot watch a run tick green, watch the agent author, or watch the interview appear — you POST and wait.
- **Evidence mostly can't render in a UI.** Run **video** is served (a single file endpoint); traces, screenshots, and before/after frames are stored only as **server-local filesystem path strings** with no endpoint serving the bytes. The incident inbox's evidence "money shot" cannot display.
- **Local-only deployment.** The provided `docker-compose.yml` brings up Postgres and the hermetic test app for the golden gate; it does **not** define a hosted API/engine service. Evidence lives on local disk (`LocalDiskStore`); there is no object store, TLS, CORS, or hosting configuration.
- **No product-grade API contract.** Handlers take untyped `dict` bodies and return untyped `dict`/`list`; there are no Pydantic request/response models and thus no OpenAPI/typed client for a frontend.
- **Read-holes.** Data exists in the DB but has no read endpoint: TerritoryNotes (recon output), session health, the test catalog + version lineage, and the calibration record are all DB-only today.
- **Missing product surfaces.** No suite management (runs are ad-hoc), no pagination/filtering on lists, no interactive freeze-review, no re-baseline/pin controls, no per-test golden reference recording.

---

## 4. Requirements to reach a deployable, multi-user product

Tagged **[F]** functional / **[N]** non-functional. Ordering and rationale in §5.

**Blockers — a shippable multi-user UI cannot exist without these:**
- **[F] Authentication & operator identity.** Login, users, and authorization on every mutating action (author, launch, ruling). `ruled_by` becomes an authenticated principal. Decide the tenancy model (single-org-with-users vs multi-tenant).
- **[F] Asynchronous launch + live progress.** Launching returns a run id immediately; a step-event stream (SSE/WebSocket) or poll loop lets the UI show steps executing, the hot lane authoring, and the interview live. Touches the run-execution model.
- **[F] Artifact serving.** An endpoint (or object-store signed URLs) that serves trace/screenshots/before-after frames as fetchable URLs, so run detail and the incident inbox render real evidence.
- **[N] Hosted deployment.** A real service definition (API/engine image), object storage for evidence (MinIO/S3 behind the existing store interface), TLS/CORS, and configuration for a server rather than a laptop.

**Important — a real UI hits these immediately:**
- **[N] Typed API contract.** Pydantic request/response models + OpenAPI, so the frontend builds against a typed, generated client.
- **[F] Read endpoints for existing data:** TerritoryNotes, session health (surface "re-seed needed"), test catalog + version lineage + rollback targets, calibration record, post-hoc suite-run inspection.
- **[F] List controls:** pagination/cursoring and filtering on runs, incidents, and changelog.
- **[F] Suite management:** create/list/edit saved suites (not just ad-hoc runs).

**Reviewer & authoring surfaces the design calls for:**
- **[F] Interactive freeze-review:** see and one-click-edit the proposed network expectation and assertion scoping before a Bundle freezes.
- **[F] Reviewer actions:** pin/unpin a run, trigger re-baseline / re-freeze, view version history.
- **[F] Per-test golden reference recording:** a reference video of the intended flow (auto-seeded from the authoring run, human-overridable), shown beside the oracle to eyeball drift the oracle didn't catch. Depends on artifact serving.

**Concurrency for multiple simultaneous users:**
- **[F] Per-user/session isolation.** Today tests run against a single shared workspace with seeded per-target sessions. Concurrent users authoring/running against the same target need isolation (namespacing extends per-run today; per-user/per-workspace separation is not modeled).

---

## 5. Roadmap & sequencing

**Gate: nothing UI-facing starts until parity is green.** Polishing a UI for an unproven engine is backwards, and the biggest fixes (async/streaming, artifact serving) touch the run-execution model and would destabilize the parity path if done concurrently. First finish the live-parity corpus (close the campaign CRUD delete-leg and the campaign-from-NL residue).

Then, in order (each unblocks the next):

1. **Typed API contract first.** Pydantic models + OpenAPI. Cheap, and it makes every later change and the whole frontend safer.
2. **Artifact serving.** Unblocks the single most valuable UI moment — evidence in run detail and the incident inbox.
3. **Async launch + live progress.** The run-watching feel; the biggest "useful" lever.
4. **Read-holes + list controls + suite management.** Mostly additive endpoints; do them together.
5. **Authentication & identity.** When multi-user is a committed product decision (see §7).
6. **Reviewer/authoring surfaces:** freeze-review, pin/unpin, re-baseline, and the per-test golden reference recording (rides on #2).
7. **Hosted deployment & per-user isolation.** Object store, service image, TLS, and the concurrency model for real simultaneous users.

---

## 6. Success criteria

- **Engine (met on golden, in progress on live):** a flow authored from NL freezes, replays green in seconds with zero LLM, heals a locator drift automatically as one changelog line, and yields exactly one reviewable incident on a behavioral redesign — with a negative control that reliably reds.
- **Deployable product (target):** two people, each logged in, can author a test from the browser, **watch it run** with evidence rendering, and one of them can rule on a resulting incident — against a hosted instance, not a laptop.

---

## 7. Open product decisions

- **Tenancy & auth timing:** single-org-with-users vs multi-tenant, and when multi-user becomes real (this gates requirement §4 auth and §5 step 5).
- **Golden reference recording:** auto-seed from the authoring run vs human-uploaded source-of-truth (or both).
- **Concurrency model:** how far to take per-user/per-workspace isolation vs the current single-shared-workspace + per-run namespacing.
- **Parametrization:** multi-row param expansion (schema slot exists; single default row runs today).

---

## 8. Non-goals (current)

- Programmatic re-login / captcha solving — expiry is detected and the suite parks for a human re-seed.
- Multi-browser / multi-viewport matrices — Chromium, one viewport (schema is matrix-ready).
- Cron/internal scheduler — triggers are manual / API / deploy-hook; scheduled runs come from external CI.
- A visual test-editor — authoring is fire-and-forget with the ambiguity interview; an interactive editor is a later UI over existing Bundle states.
