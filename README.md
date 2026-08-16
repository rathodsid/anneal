# Anneal

**A target-agnostic web QA engine: an agent authors the tests, deterministic code replays them.**

Every UI change becomes either an auto-applied locator diff or a reviewed behavioral diff. Nothing
changes silently.

The name comes from metallurgy — heat, then cool into a stronger structure. That is the whole loop:
hot AI lanes produce and repair tests, which freeze into deterministic artifacts that run cold.

---

## The problem

There are two common ways to test a web app, and both are wrong in opposite directions.

**Hand-written Playwright suites** break on every UI change, and the breakage is indiscriminate — a
renamed testid and a genuinely removed feature look identical in CI.

**Live AI agents that drive the browser on every run** absorb changes silently. That is the failure
mode worth naming: in a benchmark against a real product redesign, a frozen replay broke *honestly*
(red in 29 seconds) while a live agent absorbed the same change and reported green in 252 seconds.
A test that adapts to any change no longer tests anything.

Anneal's answer is to put the intelligence in **authoring and repair**, never in the run:

```
author ──freeze──▸ BUNDLE vN ──compile──▸ replay ──red──▸ classify ──▸ heal | re-author ──▸ BUNDLE vN+1
```

## Three lanes, one artifact

Everything is a producer or consumer of one canonical artifact — the **Test Bundle**. Lanes differ
by how much intelligence is in the loop, but all run through one step-executor whose temperature is
fixed at construction.

| Lane | Temperature | What it does | Runs |
|---|---|---|---|
| **Author** | HOT — LLM drives the browser | Recon → ambiguity interview → NL agentic observe-act loop on the live app → resolved steps (locator + fallback ladder + postcondition + network expectation) → negative-control calibration → frozen Bundle | Once per test version |
| **Heal** | WARM — deterministic first | On locator drift: re-resolve via the stored fallback ladder with no LLM; LLM-over-DOM only if the ladder fails. Verify red→green, auto-apply as vN+1 with a changelog entry | On locator drift only |
| **Replay** | COLD — zero LLM | The Bundle compiles to a pure-Playwright step plan. Each step's postcondition *is* its wait. Trace, video and network log always on. Emits structured step events for deterministic routing | Every execution |

One compiler lowers the Bundle to a step plan, and `anneal export` prints that *same plan* as a
standalone Playwright script — one compiler, two backends, so the exported script cannot skew from
what actually ran.

## Design decisions worth stealing

- **Cold has no LLM in its dependency graph.** `anneal-executor` may not import `anneal-llm`, and a
  test asserts it in a clean subprocess. Intelligence is reached only through `Healer`/`Author`
  Protocols injected at one constructor — auditable in one place, not scattered mode checks.
- **Target-agnostic is gate-enforced, not aspirational.** Engine code and prompts contain zero
  app-specific literals — no routes, testids, or domain nouns. App specifics live only in
  `TargetProfile`/`TerritoryNotes` *data*. A grep guard runs before every gate, and a second guard
  catches *semantic* overfit (a domain noun smuggled into a prompt with no literal).
- **The engine never guesses an oracle.** Before freezing, the author runs an ambiguity interview and
  asks only about choices that change what the test asserts. Unanswered questions block the freeze.
  Negative-control calibration then proves the assertion can actually *fail*.
- **Volatile values are placeholders, never literals.** `{{run.token}}`, `{{run.today±Nd}}`,
  `{{param.x}}` — a closed, versioned registry resolved at compile time. An unknown placeholder is a
  compile error. Every run creates uniquely-named entities, so residue can never collide.
- **The write guardrail lives at the executor, not in a prompt.** Destructive interactions may only
  resolve against entities whose visible identity carries the current run's token.
- **The bar is frozen.** `verify.sh` SHA-256-pins the schemas and the golden fixture and refuses to
  run if they changed. "Done" means the gate exits 0 — not that the code looks done.

## Architecture

A `uv` workspace. `anneal-core` has no playwright, db, llm, or http dependency.

| Package | Role |
|---|---|
| `anneal-core` | Domain model + compiler. Bundle, step plan, ports (Protocols), compile, validate |
| `anneal-executor` | Step interpreter + browser layer. Must not import `anneal-llm` |
| `anneal-db` | SQLAlchemy Core over a raw-SQL schema + Alembic migrations + repositories |
| `anneal-artifacts` | Artifact stores (trace, video, screenshots, network logs) |
| `anneal-llm` | LLM seam: balanced-brace JSON extractor, provider fallback, param quirks |
| `anneal-scheduler` | Worker pool: SKIP-LOCKED claim, token mint, session probe gate, sweeper |
| `anneal-api` | The one API (FastAPI). CLI and console are thin clients |
| `anneal-cli` | Thin httpx client of the API |
| `anneal-authoring` | HOT lane: recon, interview, NL authoring, calibration |
| `anneal-heal` | WARM lane: ladder heal, LLM-over-DOM fallback |
| `anneal-classify` | Red-path routing, incident state machine, budgets, re-author |

The API is the single behavioral surface: `anneal run` POSTs the same endpoint a CI deploy-hook
calls, so the CLI and the React console cannot fork behavior.

## Quickstart

Requires Docker (compose v2), [uv](https://docs.astral.sh/uv/), and Node 20+.

```bash
docker compose up -d                                    # postgres (:55432) + the golden app (:8099)
uv run alembic upgrade head                             # migrations
uv run uvicorn anneal_api.app:app --port 8080           # the API
cd frontend && npm install && npm run dev               # the console (Vite, :5173)
```

For the console in dev, set `ANNEAL_CORS_ORIGINS=http://localhost:5173` on the API and point the
client at it with `VITE_API_BASE=http://localhost:8080`. For a single deployable, run
`npm run build` and set `ANNEAL_FRONTEND_DIST=frontend/dist` so FastAPI serves it same-origin.

The hot and warm lanes need an LLM key — `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in `.env` (see
`.env.example`). The cold replay lane needs neither, by construction.

## The build gate

`verify.sh` is the source of truth. "Done" means it exits 0.

```bash
./verify.sh              # every gate (m0→m4 + API contract) as a regression sweep
./verify.sh m0           # one milestone; each re-runs the lower ones
```

Every run first enforces two guards that cannot be weakened from inside: the frozen-bar integrity
check and the target-agnostic audit.

Tests are selected by pytest marker, not by path:

```bash
uv run pytest -m m1                  # one milestone's suite
uv run pytest -m api_contract        # typed API + OpenAPI gates
```

**Generalization is proven on flows the engine has never seen.** The holdout gate points at a
directory *outside* this repo and authors each unseen NL flow through the hot lane, asserting it
replays to its expected verdict:

```bash
ANNEAL_HOLDOUT_DIR=/path/to/flows ./verify.sh holdout
```

An engine that passes only the bundled corpus is a failure even with a green gate. Bring your own
target — a `holdout.json` supplies each flow's `base_url`, persona, and expected verdict.

## Documentation

- **[GOAL.md](GOAL.md)** — the build contract: what "done" means, milestone order, the load-bearing rules
- **[HLD.md](HLD.md)** — the *why*. The frozen decision register D1–D37
- **[LLD.md](LLD.md)** — every decision lowered to a code-level contract: DDL, compiler, error table, incident machine
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the system in one read
- **[API.md](API.md)** — the live API surface

## License

MIT — see [LICENSE](LICENSE).
