# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Anneal is a **target-agnostic web QA engine**: an agent authors and re-authors tests, **code replays** them. Every UI change becomes either an auto-applied locator diff or a reviewed behavioral diff — nothing changes silently. Codename from metallurgy: hot AI lanes produce/repair tests, which freeze into deterministic artifacts that run cold.

## The three load-bearing rules (do not break)

These are gate-enforced and non-negotiable — `verify.sh` hard-fails before any milestone if violated (see GOAL.md rules 7 & 9, HLD D31):

1. **The bar is frozen — never edit it to pass a gate.** READ-ONLY: `verify.sh`, `schemas/*.json`, `fixtures/campaign-crud.bundle.jsonc`, `parity/corpus/*`, `HLD.md`, `LLD.md`. You may build the harness that *runs* checks; you may never weaken a check, lower a threshold, delete a corpus flow, or make N1 pass. If a gate looks wrong, stop and surface it — changing the bar is the one unrecoverable failure.
2. **Target-agnostic.** Engine code (`packages/*/src`) and prompts must contain ZERO app-specific literals (routes, testids, flow choreography, domain nouns like "campaign"/"concurrency"). App specifics live only in `TargetProfile` / `TerritoryNotes` **data**. Enforced by `check_target_agnostic` and `check_prompts_agnostic` in verify.sh.
3. **Cold has no LLM in its dependency graph (D31).** `anneal-executor` must not depend on or import `anneal-llm`. Intelligence is reached only through the `Healer`/`Author` Protocols in `anneal_core.ports`, injected into the warm/hot interpreter. Auditable at one constructor, not scattered mode checks.

## Fixing & improving: generalize the learning, never patch one test/platform

This engine QAs MANY platforms — the golden app is only the one bundled here. Rule 2 (target-agnostic) is not only "no app literals" in the diff; it is how to *work*:

- When a test fails, do **not** fix the symptom for that flow/platform. Find the **engine gap** — the missing capability or the wrong assumption that produced the failure — and fix **that**, so every target benefits.
- Before editing code, state the generalized learning explicitly: *"the engine assumed X; the real invariant is Y."* The change must read as a Y-for-all-targets fix, with the failing flow as merely the witness that exposed the gap.
- A change that only makes one named test / route / testid / domain noun pass is a red flag — it regresses the moment the platform changes. Prefer a general mechanism (a heuristic over observed structure, an invariant, a contract) over a special-case.
- Verify the generalization: the fix should hold on the golden app **and** the holdout — not just the flow in front of you. Spend the thinking time on "what is the gap" before touching code.

## Commands

The build gate is the source of truth — "done" means `verify.sh` exits 0, not that code "looks done":

```bash
./verify.sh              # all gates (m0→m4 + api contract), regression sweep
./verify.sh m0           # single milestone (m0|m1|m2|m3|m4); each re-runs lower ones
ANNEAL_HOLDOUT_DIR=/path ./verify.sh holdout   # opt-in: generalization on unseen flows
```

Tests use pytest markers (see `[tool.pytest.ini_options]` in pyproject.toml), not paths:

```bash
uv run pytest -m m1                 # one milestone's suite
uv run pytest tests/test_m1.py -k some_name   # a single test
uv run pytest -m api_contract      # typed API + OpenAPI gates (A0)
```

Run the stack locally:

```bash
docker compose up -d                                   # postgres (host :55432) + golden app (:8099)
uv run alembic upgrade head                            # migrations
uv run uvicorn anneal_api.app:app --port 8080          # the API
cd frontend && npm install && npm run dev              # the console (Vite, :5173)
```

- Dev CORS: set `ANNEAL_CORS_ORIGINS=http://localhost:5173` on the API; point the client with `VITE_API_BASE=http://localhost:8080`.
- Prod frontend: `npm run build`, then set `ANNEAL_FRONTEND_DIST=frontend/dist` → FastAPI serves it same-origin (one deployable). Unset → `/` returns a plain-text pointer to this setting.
- After a backend change, regenerate the typed client: `cd frontend && npm run gen:api` (dumps `/openapi.json` → `src/api/schema.d.ts`). Never hand-write fetch shapes.
- Project id in dev/golden is `"golden"`. `./verify.sh m1` leaves seeded data.

Config: `ANNEAL_DB_URL` (default `postgresql+psycopg://anneal:anneal@localhost:55432/anneal`), `ANNEAL_ARTIFACTS` (default `./.anneal/artifacts`), `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` in `.env`. LLM default for hot/warm lanes is a top-tier Claude model (e.g. `claude-opus-4-8`) with OpenAI as the seam's fallback.

## Architecture: three lanes, one artifact

Everything is a producer or consumer of one canonical artifact — the **Test Bundle** (`bundle.json`). Lanes differ by "temperature" (how much intelligence is in the loop), but all run through **one step-executor** whose temperature is fixed at construction:

- **Author (HOT, LLM drives browser)** — recon→TerritoryNotes → ambiguity interview → NL agentic observe-act loop → resolved steps (locator + fallbacks + postcondition + network expectation) → negative-control calibration → frozen Bundle. Also runs re-authoring on behavior change. Package: `anneal-authoring`.
- **Heal (WARM, deterministic-first, LLM fallback)** — on locator drift: re-resolve via the stored fallback ladder (no LLM); LLM-over-DOM only if the ladder fails. Verify red→green, auto-apply as vN+1 with a changelog entry. Drains all drifts in one session. Package: `anneal-heal`.
- **Replay (COLD, zero LLM)** — Bundle compiles to a pure-Playwright step plan; each step's postcondition *is* its wait. Tracing/video/network always on. Emits structured `step_event`s (step id, rung used, error class) for deterministic routing. Runs every execution — seconds, not minutes. Package: `anneal-executor`.

Flow: `author ──freeze──▸ BUNDLE vN ──compile──▸ replay ──red──▸ classify ──▸ heal | re-author ──▸ BUNDLE vN+1`.

One compiler lowers the Bundle to a step plan; `anneal export` prints the *same plan* as a standalone Playwright script (one compiler, two backends — the exported script can't skew from what ran). There is no generated-script subprocess in the execution path.

### Packages (uv workspace, `packages/*`)

| Package | Role |
|---|---|
| `anneal-core` | Domain model + compiler. **No playwright, db, llm, or http.** Bundle, stepplan, ports (Protocols), compile, validate. |
| `anneal-executor` | Step-interpreter + browser layer. **Must not import `anneal-llm` (D31).** |
| `anneal-db` | SQLAlchemy Core over raw-SQL schema (§3) + Alembic migrations + repositories. Sync engine. |
| `anneal-artifacts` | ArtifactStore impls (LocalDiskStore). |
| `anneal-llm` | LLM seam: balanced-brace extractor, provider fallback, param quirks. |
| `anneal-scheduler` | Worker pool: SKIP-LOCKED claim, token mint, session-probe gate, cold runs, sweeper. |
| `anneal-api` | **The ONE API (D32) — FastAPI.** CLI + console are thin clients; rulings are web-only. |
| `anneal-cli` | Thin httpx client of the API — everything except rulings. |
| `anneal-authoring` | HOT lane: recon, interview, NL author, calibration. |
| `anneal-heal` | WARM lane: ladder heal, LLM-over-DOM fallback. |
| `anneal-classify` | Red-path routing + incident state machine + budgets/oscillation + re-freeze/re-author. |

### One API, thin clients (D32)

`anneal-api` is the single behavioral surface. The CLI (`anneal run/status/export`) and the React console are thin clients that must not fork behavior — `anneal run` POSTs the same launch endpoint a CI deploy-hook calls. Rulings (approve/reject provisional versions) are **web-only**. There is no console/operator auth this cycle (D17) — every endpoint is open; assume localhost/trusted network.

### Data model (`anneal-db`, raw SQL in `schema.py`)

Core tables: `project`, `target`, `territory_notes`, `credential`, `session`, `module`/`module_version`, `test`/`test_version`, `test_golden`, `incident`/`incident_member`, `suite`/`suite_run`, `run_task`, `run_result`, `step_event`, `changelog`. Every test binds to **one canonical target environment**; only runs against it may mutate the artifact (heal/re-author) — other environments run read-only and are labeled `OFF_CANONICAL`. Every version carries mandatory **lineage** (parent, reason, ruling, incident, triggering run, `verified_against`).

## Where authority lives (read order)

- **GOAL.md** — the build contract: what "done" means, milestone order, the frozen inputs, the load-bearing rules. Read first.
- **HLD.md** — the *why*. Decision register D1–D37 is frozen; extend, never re-open.
- **LLD.md** — lowers every HLD decision to a code-level contract.
- **API.md** + generated `frontend/src/api/schema.d.ts** — the live API surface; these win over any prototype/README notes (they're regenerated from the running server).
- **docs/** — the backend/build specs the API and console were built against.

## Conventions worth knowing

- **Never guess an oracle or interview answer** (D26): unanswered authoring-interview questions block the freeze.
- **Volatile values are placeholders, never literals**: `{{run.token}}`, `{{run.today±Nd}}`, `{{param.x}}` — a closed, versioned registry resolved at compile time; an unknown placeholder is a compile error.
- **The write guardrail lives at the executor**, not in a prompt: destructive interactions may only resolve against entities whose visible identity carries the current run's token; `exclusive` policy is the reviewed escape hatch.
- **The action vocabulary is a versioned registry, not an enum**: an unsupported gesture fails with `UNSUPPORTED_VERB` (an engine backlog item), cleanly separated from a test failure.
- Migrations are Alembic (`migrations/versions/`); the DB URL is resolved in `migrations/env.py` from `ANNEAL_DB_URL`.
