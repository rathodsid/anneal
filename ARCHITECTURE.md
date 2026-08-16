# ARCHITECTURE.md — Anneal, for deployment & scale

> **Audience:** the engineer who has to stand this service up and grow it.
> **Scope:** the system *as built on `main`* — topology, state, config, the deploy runbook, and the
> honest scaling limits. This is a companion to `HLD.md` (the frozen decision register — the *why*)
> and `API.md` (the exact endpoint surface). Where this doc and the code disagree, the code wins;
> file references are given so you can check.
>
> **Read `HLD.md` for rationale; read this to ship it.**

---

## 1. What Anneal is (one screen)

A **target-agnostic web QA engine**. An agent authors and re-authors tests; **code replays them cold
(zero LLM)**; every UI change surfaces as either an auto-applied locator diff or a human-reviewed
behavioral diff — nothing changes silently. One canonical artifact (the **Test Bundle**) flows through
three "temperature" lanes:

| Lane | Temp | LLM? | Runs |
|------|------|------|------|
| **Author** | HOT | yes — LLM drives the browser | once per test version |
| **Heal** | WARM | deterministic-first, LLM fallback | on locator drift |
| **Replay** | COLD | **never** (D31, gate-enforced) | every execution — seconds |

All three run through one step-executor whose temperature is fixed at construction. The cold executor
has **no LLM in its dependency graph** — this is a load-bearing invariant, not a runtime flag.

---

## 2. Deployment topology (the headline)

**Today, Anneal is a single deployable: one FastAPI/uvicorn process, plus Postgres.** Everything else —
the scheduler, the browser workers, the healer, the author agent — are *libraries imported into that one
process*, not separate services.

```
                         ┌───────────────────────────────────────────────┐
   CLI / CI / browser    │  uvicorn: anneal_api.app:app   (ONE process)   │
   ───────────────────▶  │                                                │
   HTTP :8080            │  FastAPI routers  ── async ──▶ poll/SSE reads   │
                         │        │                                        │
                         │        │ POST .../runs  (202 Accepted)          │
                         │        ▼                                        │
                         │  suite-run daemon THREAD (own asyncio loop)     │
                         │        │   scheduler.run_suite()                │
                         │        ├─ probe session/env                     │
                         │        ├─ N async workers (max_parallel)        │
                         │        │    claim (SKIP LOCKED) → cold replay    │
                         │        │    via Playwright chromium              │
                         │        └─ suite-end sweeper                     │
                         └───────────────┬──────────────┬─────────────────┘
                                         │              │
                            ┌────────────▼───┐   ┌──────▼──────────────┐
                            │  Postgres 16   │   │ local disk          │
                            │  (state +      │   │ ANNEAL_ARTIFACTS/   │
                            │  coordination) │   │  <run_id>/trace,vid │
                            └────────────────┘   └─────────────────────┘
```

**Why suite runs live on a thread, not the event loop:** the DB layer is *synchronous* SQLAlchemy and
the LLM client is synchronous; running them on the API's event loop would freeze the poll/SSE endpoints.
So a launch spawns a daemon thread with its own `asyncio.run(run_suite(...))`
(`packages/anneal-api/src/anneal_api/suites_launch.py:44-56`). The API stays responsive; the run
executes beside it.

**Consequence you must design around:** because in-flight runs are daemon threads *inside the API
process*, **restarting/redeploying the API kills every running suite.** They are not resumed — they are
*reaped*: on startup the lifespan hook marks orphaned `running` suite_runs as
`parked / verdict=interrupted / park_reason=INTERRUPTED` and releases their per-target lock
(`app.py:27-38`, `repo.reap_interrupted`). Plan deploys for when the target is idle, or expect
interrupted runs to be re-launched.

### What is NOT here yet (so you don't go looking)
- **No Dockerfile for the API.** The only `Dockerfile` in the repo is `fixtures/app/Dockerfile` (the
  hermetic *golden test app*, used by `verify.sh`). `docker-compose.yml` brings up **only** Postgres
  (host `:55432`) and the golden app (`:8099`) — **not** the API. You will write the API's container
  image / process supervision. See §7.
- **No separate worker or scheduler daemon.** `anneal-scheduler` and `anneal-executor` declare no
  entrypoints (`[project.scripts]`); only `anneal-cli` does (`anneal = anneal_cli.main:main`, a thin
  httpx client). There is no independently scalable worker tier.
- **No object store.** Artifacts are local-disk only (§5).
- **No migrations-on-boot.** Alembic is run by hand (§7).

---

## 3. Runtime components (what's in the one process)

| Package | Role in the running system |
|---|---|
| `anneal-api` | The ASGI app (FastAPI). Hosts every endpoint **and** spawns the suite-run threads. |
| `anneal-scheduler` | `run_suite()` — probe gate, worker pool, claim loop, sweeper. Imported, not a daemon. |
| `anneal-executor` | The step-interpreter + Playwright browser layer. Runs cold replay. **Must not import `anneal-llm` (D31).** |
| `anneal-core` | Domain model + compiler (Bundle → step plan). No playwright/db/llm/http. |
| `anneal-db` | Sync SQLAlchemy Core over raw SQL + Alembic + repositories + the credential vault. |
| `anneal-artifacts` | `LocalDiskStore` (the only ArtifactStore impl). |
| `anneal-llm` | LLM seam (Anthropic primary, OpenAI fallback). Only reachable from HOT/WARM lanes. |
| `anneal-authoring` / `anneal-heal` / `anneal-classify` | HOT author, WARM heal, red-path routing. |
| `anneal-cli` | Separate deployable: a thin httpx client of the API (`anneal run/status/export/author/select`). |

**Coordination is entirely in Postgres.** There is no Redis, no message broker, no external queue. The
claim queue (`run_task` rows), the exclusive-test lock, the launch lock, and the sweeper's token
allowlist all live in the database.

---

## 4. Request & run lifecycle (end to end)

1. `POST /projects/{p}/suites/{suite}/runs?target_id=…` → **202 `{suite_run_id}`** immediately. The
   INSERT of the `suite_run` row *is* the launch lock: a partial unique index
   `one_active_suite_run_per_target` (on `state ∈ pending|probing|running`) makes a second launch on a
   busy target collide → **409** with `{error: "target busy", active_suite_run_id}` (no TOCTOU).
2. A daemon thread runs `scheduler.run_suite()`:
   - **Expand** one `run_task` per suite member (empty suite → `parked / EMPTY_SUITE`).
   - **Probe gate** (state `probing`): validate the seeded target-app session + env health *before any
     test runs*. Env down → `parked_env` (`ENV_INFRA`/`ENV_CAPACITY`). Auth expired → one soft
     re-login then re-probe, else `parked_auth` (`AUTH_EXPIRED`), with `needs_human` on captcha/OTP/SSO.
   - **Run** (state `running`): launch `min(max_parallel, target.max_parallel)` async workers (default
     4). Each worker loops: **claim a `run_task` via `SELECT … FOR UPDATE SKIP LOCKED`** → (if
     `exclusive`, take a Postgres **advisory lock** on the target) → `_run_one`.
   - `_run_one`: create the `run_result` up front → compile the Bundle → **cold replay** through the
     interpreter inside `recording()` (trace + video + screenshot always on), streaming `step_event`
     rows live. Non-green → **one automatic retry** (retry-green ⇒ `FLAKY`), else classification routes
     to heal / re-author / review.
   - **Suite-end sweeper**: deterministically deletes residue — replays recorded DELETE endpoints for
     entities carrying this run's token; never fails the suite.
3. Watch progress: **poll** `GET /suite_runs/{id}` to a terminal `state` or **stream**
   `GET /suite_runs/{id}/events` (SSE, ~250ms tail of `step_event` rows). SSE streams are per-request
   async generators, not background tasks.

Verdict precedence (D29): `red > parked_auth > parked_env > provisional > green_with_heals > green`.

Full endpoint surface: **`API.md`** (authoritative, pinned by `tests/test_api_contract.py`).

---

## 5. State & storage

### Postgres (the system of record + the coordinator)
- **Engine:** Postgres 16, **synchronous** psycopg 3 driver, sync SQLAlchemy Core with
  `pool_pre_ping=True` (`anneal-db/.../engine.py`). Deliberately sync — "v1 laptop scale; brief sync DB
  calls from the async scheduler are acceptable." This is the documented scaling caveat (§6).
- **Schema is raw SQL** in `anneal-db/.../schema.py` (one `DDL` string), applied verbatim by migration
  `0001`. ~18 tables: `project`, `target`, `territory_notes`, `credential`, `session`, `module` /
  `module_version`, `test` / `test_version`, `test_golden`, `incident` / `incident_member`, `suite` /
  `suite_run`, `run_task`, `run_result`, `step_event` (bigserial, the live trail), `changelog`. Uses
  the `pgcrypto` extension.
- **Coordination primitives (all in-DB):**
  - `run_task` claim queue — `FOR UPDATE SKIP LOCKED LIMIT 1`, ordered `exclusive desc, id`.
  - **Launch lock** — partial unique index `one_active_suite_run_per_target`.
  - **Exclusive-test serialization** — session-scoped `pg_try_advisory_lock` per target.
  - **Incident convergence** — partial unique index `incident_open_cluster` so N red runs collapse
    into one open incident.

### Artifacts (evidence)
- **`LocalDiskStore` only.** Trace zip, `video/webm`, screenshots written under **`ANNEAL_ARTIFACTS`**
  (default `./.anneal/artifacts`), one dir per run; `run_result.*_path` hold absolute local paths.
  Served by the single path `GET /runs/{id}/artifacts/{kind}` (`kind ∈ trace|video|screenshot|network_log`).
- **No S3/MinIO implementation exists** — only the `ArtifactStore` Protocol seam and a "MinioStore is the
  later swap" comment. **This is the #1 blocker to multi-instance scale** (§6).

### Secrets
- **Credential vault:** target-app logins are stored **Fernet-encrypted at rest** in
  `credential.secret_enc`, keyed by **`ANNEAL_VAULT_KEY`** (`anneal-db/.../vault.py`). No plaintext
  fallback — a missing/invalid key *refuses* the operation (`VaultKeyMissing`). Resolution order: DB
  (decrypted) → per-ref env `ANNEAL_<REF>_USERNAME/_PASSWORD` → generic `ANNEAL_UI_USERNAME/_PASSWORD`.
- **`ANNEAL_VAULT_KEY` is stateful:** rotate it and existing encrypted credentials become
  undecryptable. Treat it like a database — back it up, store it in a secret manager, don't regenerate.
- These credentials are the **tested apps' logins**, not Anneal operator identities (there are none — §8).

---

## 6. Scaling

**The model:** one API process = API + scheduler + browser workers, coordinated through Postgres. The
scaling story has two axes.

### Within one instance
- **`target.max_parallel`** (per-target, clamped by the scheduler default of 4) sets how many suite
  members replay concurrently. This is your main throughput knob per target.
- Concurrency is **browser-bound, await-interleaved** — the sync DB claims still serialize (documented
  in `scheduler.py`). Each run task gets a fresh `browser.new_context()`; there is **one
  `chromium.launch()` per suite run** and **no cross-run context pool**. Playwright/Chromium is the real
  CPU/RAM cost — size instances by concurrent browser contexts, not by request rate.
- **One active suite run per target** (launch lock). A target is a serialization boundary: you cannot
  run two suites against the same target at once, by design (shared workspace, §7 of HLD).

### Across instances (horizontal)
Running **multiple API replicas is *safe by construction*** — SKIP-LOCKED claims, advisory locks, and
the per-target unique index mean replicas won't double-claim work or double-launch a target. **But two
things must be true first:**

1. **Shared artifact storage.** `LocalDiskStore` writes to a local path. With >1 replica, a trace
   written by replica A is unreadable by replica B. Until the MinIO/S3 store lands, you **must** point
   `ANNEAL_ARTIFACTS` at a shared network volume (NFS/EFS) mounted identically on every replica — or run
   a single instance. This is the hard gate on horizontal scale.
2. **Deploy discipline for in-flight runs.** A replica bounce kills its suite-run threads (they're
   reaped as `INTERRUPTED`, not migrated). Rolling deploys will interrupt runs on the drained replica;
   they need re-launch.

### Known bottlenecks / limits (tell your lead these up front)
| Limit | Where | Impact |
|---|---|---|
| Local-disk artifacts only | `anneal-artifacts` | Blocks >1 replica without shared FS. |
| Sync DB from async scheduler | `anneal-db/engine.py` | DB claims serialize; fine at "laptop/team" scale, revisit for large fleets. |
| In-process runs die on restart | `suites_launch.py` | No run durability across deploys; reaper cleans up, doesn't resume. |
| No context pool; browser per task | `scheduler.py`, `evidence.py` | Chromium launch/teardown cost per run; RAM-bound. |
| Sweeper uses first persona only | `scheduler.py` | Multi-persona/workspace cleanup is a noted follow-up. |
| Retention prune unwired | see §8 | Runs/evidence accumulate unbounded until you schedule it. |

---

## 7. Deploy runbook

### Prerequisites
- Python **3.12–3.13**, `uv` (workspace manager), Node (to build the frontend), a Postgres 16 you
  control, and Chromium deps for Playwright.

### Steps
```bash
# 1. Dependencies (uv workspace, 11 packages)
uv sync --frozen
uv run playwright install chromium          # + OS deps: playwright install-deps chromium

# 2. Database
#    Point ANNEAL_DB_URL at your managed Postgres (NOT the compose one in prod).
export ANNEAL_DB_URL="postgresql+psycopg://user:pass@host:5432/anneal"
uv run alembic upgrade head                  # applies migrations 0001..0009; run on every deploy

# 3. Secrets (see §5 / §8)
export ANNEAL_VAULT_KEY="<fernet-key, backed up>"
export ANTHROPIC_API_KEY="<...>"             # OPENAI_API_KEY optional (fallback provider)

# 4. Frontend (same-origin, one deployable)
cd frontend && npm ci && npm run build       # emits frontend/dist
export ANNEAL_FRONTEND_DIST="frontend/dist"  # FastAPI mounts the SPA at /

# 5. Artifacts (shared volume if you run >1 replica — see §6)
export ANNEAL_ARTIFACTS="/var/lib/anneal/artifacts"

# 6. Serve
uv run uvicorn anneal_api.app:app --host 0.0.0.0 --port 8080
```

- **Local Postgres for dev only:** `docker compose up -d postgres` gives you the `:55432` instance the
  default `ANNEAL_DB_URL` points at. Do not use compose Postgres in production.
- **Containerizing:** there is no API Dockerfile yet — you'll author one (base image with Python 3.12 +
  Playwright/Chromium is the fiddly part; `mcr.microsoft.com/playwright/python` is a reasonable base).
- **Process supervision:** uvicorn single-process is the current shape. Do **not** naively add uvicorn
  `--workers N` expecting more throughput safely without first satisfying the shared-artifact
  requirement (each worker is a separate process = the same multi-instance constraints as §6).
- **CORS:** only needed if the SPA is served cross-origin (dev Vite). Same-origin via
  `ANNEAL_FRONTEND_DIST` needs none. Set `ANNEAL_CORS_ORIGINS` (comma-separated) otherwise.

### Health / smoke
- **Liveness:** `GET /` returns 200 (SPA or transitional console).
- **DB reachable:** `GET /projects/golden/targets` returns JSON (no 5xx).
- **Full gate (dev/CI, not prod):** `./verify.sh` — the source of truth for "the build works."

---

## 8. Operations

- **Auth / tenancy: there is none.** Every endpoint is open by decision (D17) — no API keys, no bearer,
  no operator identity; `ruled_by` is free-text provenance. **Do not expose this beyond a trusted
  network.** Put it behind your own gateway/VPN/SSO if it must be reachable. Org is single/implicit;
  `project.id` is the top key.
- **Secret hygiene:** provide `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `ANNEAL_VAULT_KEY` via your
  secret manager or the runtime env — **do not bake them into the image.** The repo's `.env` is
  gitignored (not committed), but a local `.env` with real-looking keys exists in the working tree;
  rotate anything that has left a laptop, and never copy `.env` into a build context.
- **Retention pruning is implemented but NOT scheduled.** `prune_runs(engine, older_than_days=7)` exists
  as a library function (`anneal-db/.../repositories.py`) — the intended 7-day sweep that deletes
  `step_event` rows + evidence, keeping pinned runs and incident-referenced runs. **Nothing calls it in
  production.** You must wire it (a cron/k8s CronJob invoking a small script, or a scheduled task) or
  Postgres and the artifact disk grow unbounded. Note: it removes DB rows/paths but **disk-side artifact
  file deletion is not yet implemented** — budget for a companion file GC on the artifacts volume.
- **Crash recovery:** the startup reaper terminal-izes interrupted runs (§2) — expected and automatic.
- **Session expiry:** the pre-suite probe parks a suite as `AUTH_EXPIRED` (loudly) *before* any test or
  agent runs, so a dead target-app cookie never gets misclassified as product bugs. Re-seeding target
  sessions is a human task (programmatic re-login / captcha is out of scope).
- **Observability:** every run is fully forensic (trace/video/network/screenshots). Engine self-metrics
  (heal rate, fallback-rung frequency, MTTG, flake rate, LLM spend per incident) are derivable from the
  ledger — no external APM is wired; add your own around the uvicorn process and Postgres.
- **Triggers:** `manual | api | deploy_hook` only — **no cron for suite runs** (D21). A CI deploy hook
  POSTs the same launch endpoint the CLI and console use.

---

## 9. Configuration reference

All config is plain `os.getenv` (no pydantic settings). Complete list:

| Variable | Default | Consumed by | Purpose |
|---|---|---|---|
| `ANNEAL_DB_URL` | `postgresql+psycopg://anneal:anneal@localhost:55432/anneal` | db, migrations | Postgres DSN. **Set in prod.** |
| `ANNEAL_VAULT_KEY` | *(none — refuses secrets if unset)* | db/vault | Fernet key for credential encryption. **Stateful — back up.** |
| `ANNEAL_ARTIFACTS` | `./.anneal/artifacts` | api, authoring | Evidence dir. **Shared volume for multi-replica.** |
| `ANNEAL_FRONTEND_DIST` | *(unset → transitional console)* | api | Dir of built SPA to mount at `/`. Set to `frontend/dist` in prod. |
| `ANNEAL_CORS_ORIGINS` | `""` | api | Comma-separated CORS allowlist (dev cross-origin only). |
| `ANTHROPIC_API_KEY` | *(none)* | llm | Primary LLM provider key (HOT/WARM lanes). |
| `OPENAI_API_KEY` | *(none)* | llm | Fallback provider key. |
| `ANNEAL_LLM_PROVIDER` | `anthropic` | llm | Primary provider. |
| `ANNEAL_LLM_FALLBACK` | `openai` | llm | Fallback provider. |
| `ANNEAL_ANTHROPIC_MODEL` | `claude-opus-4-8` | llm | Model for hot/warm lanes. |
| `ANNEAL_OPENAI_MODEL` | `gpt-4o` | llm | Fallback model. |
| `ANNEAL_<REF>_USERNAME` / `_PASSWORD` | *(none)* | vault | Per-credential env override. |
| `ANNEAL_UI_USERNAME` / `_PASSWORD` | *(none)* | vault | Generic credential fallback. |
| `ANNEAL_API` | `http://localhost:8080` | **CLI only** | Where the `anneal` CLI points. |
| `VITE_API_BASE` | — | frontend build | Dev-only; where the SPA calls the API. |

---

## 10. Pre-production checklist

- [ ] Author an API container image (Python 3.12 + Playwright/Chromium).
- [ ] Provision managed Postgres 16; set `ANNEAL_DB_URL`; run `alembic upgrade head` in the deploy pipeline.
- [ ] Generate + safely store `ANNEAL_VAULT_KEY` (backed up; loss = unrecoverable credentials).
- [ ] Inject LLM keys via secret manager; confirm no `.env` in the image/build context.
- [ ] Decide storage: single instance with local disk, **or** shared volume + multiple replicas (needed for HA).
- [ ] Put the open API behind a gateway/VPN/SSO — it has **no auth**.
- [ ] Wire the retention prune (schedule `prune_runs`) + an artifacts-disk file GC.
- [ ] Set a deploy policy that accounts for in-flight runs being interrupted on restart.
- [ ] Build + serve the frontend (`ANNEAL_FRONTEND_DIST`); confirm same-origin (no CORS needed).
- [ ] Add process/DB monitoring around uvicorn and Postgres.

---

*Grounded in a full read of `main` (packages `anneal-api`, `anneal-scheduler`, `anneal-executor`,
`anneal-db`, `anneal-artifacts`, `anneal-llm`), `docker-compose.yml`, the 9 Alembic migrations, and
`API.md`. `HLD.md` (frozen decision register D1–D37) remains the source of design intent; this doc
describes the running system.*
