# Anneal — QA Engine HLD

**Scope:** target-agnostic web QA engine, built from scratch.
**Companion:** LLD.md lowers every decision here to a code-level contract.
**Rendered copy:** https://claude.ai/code/artifact/3f3ec532-c442-47b4-867f-73baf9a0c247 (this file and the artifact are the same document; kept in sync).

> **Thesis:** An agent authors and re-authors; **code replays**; every UI change becomes either an auto-applied locator diff or a reviewed behavioral diff — and nothing changes silently.

**Codename** from metallurgy — heat, then cool into a stronger structure — because that is the engine's whole loop: hot AI lanes produce and repair tests, which are frozen into deterministic artifacts that run cold. It is grounded in a comparative benchmark against a real product redesign in which frozen replay broke honestly (red in 29s) while a live agent absorbed the same change silently (green in 252s). Anneal is built so that day produces one clustered, reviewable diff instead of either outcome.

---

## 1. Principles

1. **Deterministic by default, intelligent on demand.** The steady-state run is pure Playwright — no LLM, no capture overhead, seconds not minutes. AI runs at exactly three moments: authoring, healing, and re-authoring after a behavioral change.
2. **Nothing changes silently.** Every mutation to a test — a healed selector, a re-authored span — lands as a versioned, diffable changelog entry. The suite may fix itself; it may never hide that it did.
3. **Target-agnostic, integration-optional.** Zero app-specific code paths outside one `TargetProfile` config. The core works source-blind on any web app; repos, PRDs, PRs, and tickets plug in later as additive *signal providers*, never as dependencies.
4. **The failure signal routes; labels alone are useless.** Classification exists to send a failure somewhere — heal, re-author, retry, park, or a human — deterministically wherever the evidence allows.
5. **Evidence is free; collect it always.** Playwright tracing, video, screenshots, and network logs cost nearly nothing at run time. Every run is fully forensic; the viewer is a join, not a collector.

---

## 2. Architecture: three lanes, one artifact

Everything in Anneal is a producer or consumer of one canonical artifact — the **Test Bundle** (§3). The lanes differ by temperature: how much intelligence is in the loop.

| Lane | Temperature | What it does | Runs |
|------|-------------|--------------|------|
| **Author** | HOT — LLM drives the browser | Recon (TerritoryNotes) → ambiguity interview → NL-driven agentic observe-act loop on the live app → resolved steps with locator + fallbacks + postcondition + network expectation → calibration pass → frozen Bundle. Also runs re-authoring when behavior changes. | Once per test version |
| **Heal** | WARM — deterministic first, LLM fallback | On a locator-class failure: re-resolve via the ladder using authoring-captured fallbacks (no LLM); LLM-over-DOM only if the ladder fails. Verify red→green, then auto-apply as version n+1 with a changelog entry. | On locator drift only |
| **Replay** | COLD — zero LLM; passive evidence only (no in-loop `capture()`) | The Bundle compiles to a pure-Playwright step plan; the step-executor runs it in cold mode — zero-LLM by construction. Each step's postcondition *is* its wait. Tracing, video, network log always on. Emits structured step events (step id, rung used, error class) for deterministic routing. | Every execution — seconds, not minutes |

```
author ──freeze──▸ BUNDLE vN ──compile──▸ replay ──red──▸ classify
       ──▸ heal (locator) or re-author (behavior) ──▸ BUNDLE vN+1 ──▸ replay …
```

### One executor, three temperatures

All three lanes run through a single step-executor whose temperature is set at construction: **cold** is built with no LLM client in its dependency graph — any code path requesting one raises immediately (hard red + incident), so zero-LLM is auditable at one constructor, not scattered mode checks; **warm** may pause at a failing step with the browser still open, heal, retry the step, and continue — which is what makes drain-all-drifts-in-one-session real instead of one crash-and-rerun cycle per drift; **hot** hands the loop to the authoring agent. One compiler lowers the Bundle to a deterministic step plan (locator ladders carried as rung lists, postconditions lowered to waits, tokens substituted); the interpreter executes that plan, and `anneal export` prints the *same plan* as a standalone Playwright script whenever a human wants to read or hand-run one — one compiler, two backends, so the exported script can never skew from what actually ran. The export vendors the ~30-line rung resolver verbatim as a prelude, so exported steps stay one line each. The cold executor is a pure function of (step plan, page): same Bundle, same app state → same actions, every time. There is no generated-script subprocess anywhere in the execution path.

### Why postcondition-as-wait matters twice

The benchmark's frozen test spent roughly a third of its wall time in fixed `1500ms` sleeps — simultaneously too slow when the app is fast and too short when it is slow. In Anneal the authoring agent already proves a postcondition for every step; the compiler lowers that same postcondition into the step's wait (element state, URL match, or network response), with a per-step budget. One mechanism buys speed, flake resistance, and a machine-checkable definition of "this step worked."

### The write guardrail

Two of the three lanes hand a live browser and real credentials to an LLM whose job includes clicking Delete buttons. The guardrail lives at the **executor** level — not in a prompt: destructive interactions (deletes, dialog confirms, bulk operations) may only resolve against entities whose visible identity carries the current run's token. Anything else is refused and surfaced as a policy stop on the run. This binds the author, heal, and re-author lanes identically; the suite-end sweeper (§7) obeys it by construction; tests that must mutate shared, non-namespaceable state declare that scope explicitly via the `exclusive` policy and are reviewed as such.

### Territory knowledge and the authoring interview

Prior attempts at this broke because flow choreography — the delete-via-row-menu steps, the combobox handling — was hardcoded into prompts: brittle on any new flow, unshippable to a new target. Anneal moves that knowledge into **TerritoryNotes**: a per-target, versioned, human-editable record of routes, widget idioms, naming conventions, and known volatile regions, written by a read-only recon pass and refreshed when stale — where *stale* is defined, not vibes: any behavior-class incident touching a route marks that route's notes stale, and a periodic recon cadence refreshes the rest. Every authoring and re-authoring run is seeded with it; prompts stay general forever.

Before freezing, the author agent also runs a short **ambiguity interview**: it asks the human only about choices that would change what the test asserts (verify-in-list vs verify-gone, which literal name, which persisted outcome matters). Unanswered questions block the freeze — the agent never guesses an oracle. Together with negative-control calibration (§6) this closes both halves of oracle trust: the interview picks the *right* assertion; calibration proves it can actually *fail*.

### Multi-drift resilience

A strictly post-run heal lane discovers one drift per run: drifts at steps 2, 9, and 15 cost three red→heal→rerun cycles. Anneal fixes this in two places without ever putting an LLM in the replay lane:

- **The ladder is compiled into the frozen step plan.** The Bundle already stores fallbacks; the compiler carries them into the plan as an ordered rung list (testid, else role+name, else text), and the step-executor resolves them via a rung-priority helper: `resolve(page, rungs) → (locator, rung_used)`. Not `Locator.or()` — that is an unordered union with no priority, no telemetry about which rung matched, and a strict-mode throw when two rungs match different elements. The resolver sweeps the full ladder every ~100–250ms until the step budget (hand-rolled resolution forfeits Playwright auto-waiting, so a single sweep would let a late-rendering testid fall through to the text rung and file a spurious incident), taking the highest-priority rung with `count()==1` within a sweep. A late primary can still be shadowed by an early fallback match — acceptable, because the resulting `rung_used=fallback` surfaces it and the re-freeze fixes it. A step that resolves via a fallback rung still passes — and its step event records `rung_used=fallback`, which auto-opens a heal incident so the primary selector is re-frozen. The "escalation" is deterministic code that was always in the plan.
- **The heal lane drains all drifts in one session.** After healing the first drifted step, the (LLM-permitted) heal lane continues the same verify run and heals subsequent drifts before returning to cold replay. A multi-drift day costs one heal session, not N cycles.

**Heal-lane page state:** by the time classification routes a red (off `step_event` rows), the cold run's browser is dead. A heal is therefore always a **fresh warm-mode run** that cold-replays to the failure point — deterministic by construction — then pauses with the browser open and drains onward through the remaining drifts. There is no "attach to the failed browser" path.

**Ambiguity rule:** a fallback rung may act only if it resolves to exactly one element at replay time (`count()==1`); an ambiguous fallback fails the step as locator-class rather than gambling on the first match. The postcondition-as-wait is the backstop — a wrong-element click that "succeeds" still fails its postcondition. Both rules together keep the ladder from reintroducing the stale-wrong-click failure the parents' escalation logic existed to prevent.

Green therefore means: "frozen replay passed, possibly via deterministic fallback rungs — every one of which is on the ledger." A fallback or heal that recurs on the same step signature (flake, or an A/B-variant UI impersonating drift) trips the oscillation stop: quarantine + incident, no ping-pong.

---

## 3. The Test Bundle

One record per test version: `bundle.json` (canonical), the compiled step plan (derived, disposable — regenerated from the JSON, and printable as a standalone Playwright script via `anneal export`), and the calibration record. The JSON is the only thing lanes edit.

```jsonc
{
  "schema_version": 1,
  "project_id": "acme",
  "test_id": "campaign-crud",
  "version": 7,
  "state": "frozen",          // draft → calibrated → frozen; only frozen versions replay
  "persona": "member",         // binds one project credential
  "params": {},                // {{param.x}} slots; v1 runs the single default row
  "lineage": { "parent": 6, "reason": "behavior_change",
               "ruling": "provisional", "incident": "inc_0217",
               "triggered_by": "run_8841", "verified_against": "acme-prod-qa" },
  "intent": {
    "sources": [ { "kind": "nl", "verbatim": "…user's words, updated on re-freeze…" } ],
    // future kinds: "prd", "pr", "ticket" — additive signal providers
    "descriptor": "user can create, configure, and delete a campaign"
  },
  "policy": { "exclusive": false, "mutating": true },
  "steps": [
    { "module": { "ref": "login", "version": 4 } },   // first-class module pin
    {
      "id": "s6", "intent": "submit the create form",
      "action": "click",
      "target": { "strategy": "testid", "selector": "btn-campaign-create-submit",
                  "fallbacks": { "role": "button", "text": "Create" } },
      "postcondition": { "all": [
        { "kind": "url",     "match": ".*/campaign-v2/[^/]+" },
        { "kind": "network", "method": "POST", "path": "/api/v2/campaigns", "status": "2xx" }
      ], "budget_ms": 10000 }
      // reserved postcondition kinds: "eventually" {within_ms, poll_ms} · "effect_readback" {api query} — reserved, not v1
    },
    { "id": "s4", "action": "fill", "arg": "Campaign {{run.token}}", "…": "…" }
  ],
  "calibration": { "s6": { "negative_control": "failed_on_pre_state" } }
}
```

- **Volatile values are placeholders, never literals.** `{{run.token}}` is substituted at replay time, so every run creates uniquely-named entities and a failed run's residue can never collide with the next run. (The benchmark caught an engine freezing the token at authoring — three identical orphan campaigns from three replays. This field exists because of that screenshot.) Placeholders form a closed, versioned registry: `{{run.token}}` · `{{run.today}}` · `{{run.today±Nd}}` · `{{param.x}}`, resolved at compile time in the TargetProfile's timezone — an unknown placeholder is a compile error, never a literal that leaks through.
- **Lineage is mandatory.** Version, parent, reason (`authored` / `locator_heal` / `behavior_change`), ruling state, incident reference, the run that triggered the change, and the environment it was verified against. Every run records the version it executed (§12), so "is Monday's pass comparable to Friday's if the test changed Wednesday?" is a query, and "roll back" always has a concrete target: the parent version. Runs are deterministic *and* the thing being run has a deterministic history.
- **Intent sources are a list.** NL today; PRD, PR, and ticket references later. Re-freezing a test updates the NL alongside the steps so the intent statement never describes a dead UI.

### One canonical environment

A heal is a fact about **one environment at one time**: a selector healed against prod-QA today is wrong on stage, a preview deploy, or a rollback of prod-QA itself. Rather than keying every version per environment (branch-and-merge semantics nobody wants in v1), Anneal declares an invariant:

> **Each test binds to one canonical target environment.** Only runs against the canonical environment may mutate the artifact — heal, re-author, provisional update. Runs against any other environment execute **read-only**: they report pass/fail with full evidence but never write a version, open no incidents that auto-heal, and their failures are labeled `OFF_CANONICAL`. Repointing a suite permanently is an explicit **re-baseline** operation, not a config edit.

The schema carries the environment dimension from day one — `verified_against` on every version, environment on every run record — so if per-environment version heads are ever genuinely needed, they are an additive evolution of existing keys, not a re-keying of the engine's most central table.

### Versioned action vocabulary

Whatever verb set gets frozen into a schema, some flow eventually needs one it doesn't have — and without a plan, that failure impersonates "authoring is broken." So the vocabulary is an explicit, versioned registry, not an enum: the Bundle carries `schema_version`; action kinds (v1: `goto` · `click` · `fill` · `select` · `upload` · `keypress` · `hover` · `assert`, plus the postcondition kinds — v1: `url` · `network` · `element(visible|hidden)` · `value_is`) are registered with the version that introduced them; the compiler declares which kinds it supports and refuses cleanly otherwise. When the author lane needs a gesture the vocabulary lacks (hover, drag, iframe, multi-tab), it fails with `UNSUPPORTED_VERB` naming the missing kind — an engine backlog item, cleanly separated from a test that failed. Adding a verb is additive schema evolution; existing Bundles never recompile.

---

## 4. The network oracle

DOM-only oracles miss the optimistic-UI bug class: the toast says saved, the backend returned 500. Anneal makes the API call part of the frozen contract for every mutating step — without snapshotting anything brittle.

### Authoring plan

1. **Record.** The authoring session taps every same-host request/response — the exact instrumentation the benchmark's conftest already proved, filtered to real XHR/fetch traffic.
2. **Attribute.** A request belongs to a step if it fires inside that step's action→postcondition window. Mutating methods (POST/PUT/PATCH/DELETE) are attributed strictly; ambient GETs are ignored.
3. **Template.** Volatile path segments normalize before freezing: numeric ids, UUIDs, and tokens become placeholders — `DELETE /api/v2/campaigns/{id}`, not `…/100000241`. Same normalization discipline as `{{run.token}}`, applied to URLs.
4. **Propose, don't assume.** At freeze, each mutating step carries a proposed expectation — method + templated path + status class, optionally one response predicate (e.g. the response echoes the run token). The freeze review shows it next to the step; deleting it is one click.
5. **Replay for free.** The expectation compiles into the step's postcondition wait (`expect_response` around the action). On the happy path it adds zero time — it *replaces* a sleep, it doesn't extend one. **Compiler ordering rule:** the `expect_response` listener must register *before* the action fires — a fast response to a listener registered after the click is a race the compiler must make impossible, not a flake to debug. The attribution-window edge rule (debounced/deferred calls that fire after the step's visible settle) is pinned as an explicit definition in the LLD, not left to intuition.

### Failure semantics it unlocks

| DOM postcondition | Network expectation | Meaning | Route |
|---|---|---|---|
| green | red | Optimistic UI over a failed API — high-value bug | `API_MISMATCH` → review |
| red | green | Backend succeeded, render didn't | `UI_BUG` → review |
| red | red | The action genuinely failed | classify normally (§9) |

**Out of scope, deliberately:** full HAR diffing, header and payload snapshots, response-body schemas. Method + templated path + status class (+ at most one predicate) is the durable core; everything richer is drift bait.

---

## 5. The red path: what happens when a test fails

The replay lane emits a structured event for every step — step id, locator rung actually used, error class — so routing is deterministic for *every* rung (testid, role, text, css), closing the gap where the parent system could only auto-classify testid failures.

**Before anything classifies: retry once.** A red replay reruns one time. Pass-on-retry is recorded as `FLAKY` with a per-test flake rate on the ledger and opens nothing; sustained flake feeds the quarantine tripwire. Only a repeatable red proceeds to classification — otherwise every animation race and eventual-consistency hiccup opens an incident and burns classification.

1. **Infra signature** — 5xx, timeout to origin, quota/limit strings, rate-limit responses → `ENV_INFRA` / `ENV_CAPACITY` → retry with backoff, then park with a distinct suite status. Never enters the review queue: deterministic replay makes environment problems look *more* like bugs, so the taxonomy must catch them first.
2. **Locator-class failure** — the target element didn't resolve; page otherwise behaves → **HEAL** → ladder re-resolve from stored fallbacks → verified red→green → **auto-apply vN+1** + changelog entry. No human in the loop; full record kept.
3. **Behavioral failure, agent CAN complete the intent via a different path** → **RE-AUTHOR** → new frozen version applied immediately as **PROVISIONAL** — the suite goes green now; a step-level before/after diff (screenshots, step intents, network changes) attaches to its **incident** (§8) in the reviewer's inbox. Approve → every provisional version in the incident becomes canonical. Reject → all roll back + one bug filed with the full evidence bundle. **Bypass guard:** if the agent's new path deviates *only at the failing step* and the old control is still present but inert (renders, does nothing), the case routes to **ASK** instead — a bug creatively routed around must never ship as a provisional feature.
4. **Ambiguous change** — the new path works AND the old frozen version still passes (a parallel path appeared) → **ASK** → the one case that always waits for a human ruling before any version changes.
5. **Behavioral failure, agent CANNOT complete the intent either** → **PRODUCT_BUG** → review queue with reproduction: failing step, trace, video, network log, and the agent's attempt transcript. Nothing is healed; step-repair never runs against a suspected bug.

> **Why "provisional" instead of blocking:** the reviewer is one person. A redesign that reds 30 tests must not hold the suite hostage until 30 rulings happen — but silently absorbing it is the trap. Provisional versions keep CI green *and* keep the change on the record, reviewable in batch (§8), reversible on rejection. The honest middle.

---

## 6. Negative-control calibration

A deterministic assert that cannot fail is worse than no assert — it manufactures false confidence forever. The benchmark produced a live example: a name-assert passed while the create form was still open, because the string matched something else on the page.

So freezing includes a calibration pass: for each assertion, the engine checks it would have **failed** on the pre-action state it just walked through (the authoring session already has those snapshots — this is a replay over recorded state, not extra browsing). An assert that also passes on the "before" state is rejected at freeze: the author lane must scope it (to the row, the region, the exact field) until it discriminates. The calibration verdict is stored in the Bundle, so "when was this oracle last proven able to fail?" is a queryable fact.

**Mechanics, pinned:** calibration is *static evaluation* of assert predicates against the stored pre-action DOM/ARIA snapshots — no live browser is involved. That means only DOM-assert kinds get the negative control. **Network expectations have no pre-state analog and get a different static check — discriminativeness**: at freeze, each expectation is evaluated against the authoring run's full recorded network log; if it matches any request *outside* its own step's attribution window (e.g. background polling also answering `GET /api/user 200`), it's flagged as a weak oracle and must be tightened or ruled in the interview before freeze. Both checks are free — they consume only evidence the authoring run already captured.

---

## 7. Parallel execution on a single shared workspace

Parallelism is the real suite-speed lever, and the constraint is fixed: one workspace per target — clients won't hand out sandboxes. Isolation therefore comes from naming and scoping, not from environment walls.

- **Namespace token.** Every run of every test gets a token unique to (suite run × test). All created entities carry it; all placeholders resolve with it. Token alphabet: lowercase alphanumeric only — regex- and word-boundary-safe (a cache normalizer defeated by underscored tokens breaking `\b` anchors is a cheap lesson, encoded here). Minting is **scheduler-owned**: the scheduler generates the token when it claims the (suite run × test) row, fixed length with enough entropy that collision handling is a uniqueness constraint on the run row, not application logic.
- **Entity-scoped oracles.** Assertions about created entities bind to the token-bearing row/region, never to page-global text or counts. Two tests mutating the same list concurrently can't see each other. (Calibration in §6 enforces this mechanically — an unscoped assert usually fails its negative control.)
- **The `exclusive` flag.** Tests that mutate shared, non-namespaceable state — settings, toggles, global configuration — declare it and are serialized under a global lock (a Postgres advisory lock — the DB is already the coordination point). Everything else runs in parallel: read-only tests freely, mutating-but-namespaced tests freely.
- **Per-target concurrency cap.** `TargetProfile` carries `max_parallel`; the scheduler queues beyond it (backpressure) so the fleet itself never causes the `ENV_CAPACITY` parks it is designed to detect.
- **Workers.** A pool of browser contexts sharing the target's seeded session (contexts are cheap; sessions are the scarce thing). Scheduler drains read-only and namespaced tests concurrently, holds `exclusive` tests for the lock.
- **Cleanup, twice.** In-test cleanup remains part of the flow (delete → verify-gone is itself coverage). A suite-end sweeper additionally removes any surviving entity whose name carries this suite run's token — the safety net for red runs that die between create and delete, which the benchmark showed is exactly how residue accumulates. **The sweeper's delete path is deterministic, no LLM:** the run's own network log already recorded the create endpoints and the ids they returned, so the sweeper replays templated `DELETE`s against those ids; where no delete route was observed, the name-pattern janitor (NUANCES A-16) is the fallback. Both paths obey the write guardrail — they only touch token-bearing entities.
- **Shared-quota awareness.** Consumable app resources (slots, seats, rate limits) are an environment failure class, not a bug class — §9's `ENV_CAPACITY` exists largely because a parallel fleet consumes quotas faster than a sequential one.

### Sessions are a component, not an assumption

Authentication was the single worst empirical failure source in the benchmark: one engine rode a manually-seeded session that silently ages, the other spent 17.8 minutes discovering its credentials didn't work. And in Anneal an expired session is uniquely dangerous, because the red path amplifies it: expiry → every postcondition fails → incidents open → re-author agents launch → each lands on a sign-in page → **PRODUCT_BUG cards get filed for a dead cookie**, with agent spend attached.

So the session is a schema entity per credential — `{ seeded_at, last_validated, state }` — and every suite run starts with a **validity probe**: load the app, detect a sign-in redirect. On failure the suite parks as `AUTH_EXPIRED` before any test executes, any classification runs, or any agent launches, and the operator gets a loud re-seed alert. Probes also re-validate on any mid-suite sign-in-shaped failure, so an expiry mid-run halts the remainder instead of misclassifying it. A mid-suite park is **cooperative cancellation**: unclaimed tests stay unclaimed, in-flight workers get a cancel signal and stop at their current step boundary (evidence flushed, run marked parked) — never a hard kill that leaves half-written rows or orphaned browser processes. Programmatic re-login (captcha solving) stays out of scope — the component's job is to make expiry cheap, distinct, and impossible to mistake for product failure.

---

## 8. Incidents: one ruling for N tests

The parents' whole red pipeline — classify, heal, gate — is per-test. Run that unchanged and one redesign becomes fifteen gate decisions and fifteen independent agent runs rediscovering the same fact. Anneal makes the **Incident** a first-class entity: red runs attach to incidents, humans rule on incidents, and rulings propagate to member tests. The review inbox lists incidents, never raw failures.

```
Incident {
  id, state: open | ruled | propagating | closed,
  cluster_key: (
    surface,          // route / templated endpoint where the failure occurred
    step_signature,   // normalized action + target descriptor (volatiles stripped);
                      // for module steps: derived from (module ref + intra-module step id),
                      // NEVER inlined position — stable across module version bumps,
                      // so incidents keep clustering through pin-and-propagate
    error_class       // locator_rung | postcondition_kind | network_status | env
  ),
  members: [ { test_id, red_run, applied_version } ],
  representative: test_id,   // the ONE member whose re-author actually runs first
  change_brief: "create form now requires Start/End Date; row menu item renamed Delete campaign → Delete",
  ruling: feature | bug | null,  ruled_by, ruled_at,
  ruling_note: "one line of why — lands here and in the changelog"
}
```

### Lifecycle

1. **Attach.** Every red run computes its cluster key and joins the matching open incident, or opens one. A redesign that reds 15 tests over an hour accumulates into one growing incident, not 15 tickets.
2. **Heal-class incidents auto-resolve.** Locator drift needs no ruling: heals auto-apply per member, and the incident closes itself as one changelog line — "*btn-create-campaign → btn-new-campaign, 12 tests healed*" — instead of twelve entries.
3. **Behavior-class incidents run ONE discovery first.** Only the **representative** member is re-authored blind. Its live walk produces the provisional version *and* the **change brief** — a compact NL statement of what the app now does differently, always including what the agent had to do *differently from the old steps* (so a bypass is visible on the card).
4. **Members go provisional immediately — ruling still pending.** Once the representative's provisional version verifies green, the remaining members re-author with the change brief injected as a hint — still one agent pass per member (their flows differ; a behavioral fix is not a copy-paste), but guided instead of rediscovering, all within the incident budget. The whole incident is green without waiting for the ruling — §5's "never hold the suite hostage" rationale, honored for the members too, not just the representative.
5. **One ruling, two outcomes.** **Feature** → every provisional version in the incident becomes canonical. **Bug** → one bug filed carrying all members as affected coverage; every provisional version in the incident rolls back. Either way the ruling carries an optional one-line **ruling note** persisted on the incident and in the changelog — a Bug ruling especially, since that rationale is what the next re-author agent (and future-you) reads when the same surface drifts again. Accepted risk, written down: a bug ruling wastes at most the budget's guided member runs — bounded, ledgered, reversible.

The cost shape this buys: **1 discovery + 1 ruling + N guided applications**, versus the parents' N blind rediscoveries + N rulings. Propagation for locator heals is exact (same selector everywhere); propagation for behavior is the brief-guided re-author — the engine never pretends a step diff from one test can be transplanted verbatim into another.

**Budgets.** Every incident carries `budget_spent {agent_runs, llm_calls}` against a per-incident cap (one discovery re-author + up to M brief-guided member re-authors + K verify runs) and a per-day LLM ceiling. An exhausted budget surfaces partial results at the inbox — never a silent retry storm. A heal or fallback that recurs on the same step signature trips the oscillation stop: quarantine the test, open an incident, spend nothing more.

---

## 9. Classification taxonomy

| Category | Detected by | Routes to |
|----------|-------------|-----------|
| `LOCATOR_DRIFT` | Deterministic — step event names the failed rung, any strategy | heal lane |
| `BEHAVIOR_CHANGE` | Postcondition unmet + re-author agent succeeds via new path | provisional re-freeze / ask if ambiguous |
| `PRODUCT_BUG` | Postcondition unmet + re-author agent also fails the intent | review queue + repro bundle |
| `API_MISMATCH` / `UI_BUG` | Deterministic — DOM vs network oracle disagreement (§4) | review queue |
| `FLAKY` | Pass-on-retry (the one automatic rerun that precedes classification, §5) | recorded + per-test flake rate; sustained flake → quarantine tripwire |
| `ENV_INFRA` | 5xx/network/timeout heuristics | retry → park |
| `ENV_CAPACITY` | Quota strings, 429s, limit responses | park + suite banner |
| `TOOL_ERROR` | Engine couldn't operate a control it resolved | engine triage, never the app's fault |

This table's real foundation is one level down: the mapping from **Playwright exceptions to `error_class`** — "element didn't resolve" vs "postcondition unmet" vs "network expectation failed" vs timeout-shaped ambiguity. That mapping is a normative table in the LLD (exception type × step phase → error_class), owned by the executor, not scattered `except` clauses; every row above assumes it exists.

The LLM adjudicates only where the table says the evidence is genuinely ambiguous; everything with a deterministic detector stays deterministic. Note what replaced the parents' gate: the human rules on *diffs and bugs*, not on raw failures — classification plus the re-author attempt has already done the feature-or-bug legwork by the time a card reaches the queue.

---

## 10. Selection and signal providers

Change-driven selection stays NL-first, exactly as validated in the parent system: an NL change description → candidate surfaces chosen from the *observed* inventory only (closed vocabulary, anti-hallucination) → per-test score from semantic match against coverage descriptors plus IDF-weighted surface overlap → cold-start inclusion for unmapped tests, with a human-readable reason per selection.

Providers extend the same pipe without replacing it: a repo diff, a PRD, a merged PR, or a ticket each reduces to additional change signals and surface hints feeding the same scorer. The surface inventory re-observes on a cadence (and opportunistically from every run's network log) so selection doesn't rot as the app changes.

---

## 11. Product surface — one API, two clients

Everything human-facing sits on one engine API; the web console and the CLI are thin clients of it and can never fork behavior. The API/deploy-hook triggers are the same surface a CI job calls.

**The web console** is the front door, four surfaces mapping one-to-one onto the lanes:

1. **Author** — type the NL flow description; the hot lane runs fire-and-forget; the ambiguity interview surfaces as blocking questions on the draft; result is a calibrated, frozen Bundle.
2. **Run & select** — launch a saved suite, or paste an NL change description and get the §10 selection preview (each candidate test with its human-readable reason) before launching.
3. **Runs** — list + run detail: step NL intent ↔ before/after frames ↔ network calls, `rung_used` per step, trace and video links.
4. **Incidents** — the inbox (§8): change brief, before/after evidence, provisional members; approve/reject rulings (with the one-line ruling note) and the re-baseline operation live here, **web-only**. Reference design: the mock at https://claude.ai/code/artifact/2c288ad6-26d9-48a2-a4f0-8d281ab69d41, with real evidence links promoted above the frames and per-member expandable step diffs.

**The CLI** covers everything except rulings — author (the ambiguity interview becomes terminal prompts), run, select, status/results, `anneal export`, re-baseline — so any of it can run locally or from scripts and CI. Ruling on visual before/after evidence is deliberately excluded: it belongs in front of screenshots and video, not a terminal.

---

## 12. Observability and retention

**Every run, always:**
- Playwright trace (per-action before/after DOM + screenshots), video, network log, final screenshot
- Structured step events joined to Bundle step intents — written as `step_event` rows directly by the interpreter as each step completes, so run detail is live, not post-hoc; the viewer shows NL intent · before/after frames · network calls per step, for cold replays, not just agent runs
- Run meta: version executed, target environment, token, worker, durations per step; results keyed on (test, version, param_row, browser, viewport, environment) — the last three constant in v1 

**Retention and evidence store:**
- v1 runtime is laptop Docker compose; evidence lives on a local-disk volume behind a thin **ArtifactStore** interface, artifact paths tracked as DB rows (minio/S3 = later config swap, same interface)
- Runs pruned after **7 days** — one sweep deletes `step_event` rows and evidence files together, pinned runs excluded by query
- The **latest run of every test is pinned** — exempt from pruning, always includes video
- Changelog and lineage are permanent (metadata, not artifacts)

**Engine self-metrics** (derived from the ledger, no extra instrumentation): heal success rate, fallback-rung frequency, mean-time-to-green, flake rate per test, LLM spend per incident — the numbers that tell you whether to trust the machine.

---

## 13. Explicitly deferred

- **Programmatic re-login** — captcha solving stays out; the session component (§7) detects expiry and parks instead. Seeding remains a human task per target.
- **`assert_eventually` and `effect_readback`** — postcondition kinds reserved in the schema; implemented when a latency flow or an API read-back oracle actually needs them.
- **Engine regression harness + fixture app with drift toggles** — a stated requirement of this design, built by a parallel effort, not by Anneal itself. The engine's contracts (Bundle schema, step events, TargetProfile) are designed to be exercised by it.

---

## 14. Data model

The entity hierarchy every table keys off :

```
Org (single, implicit for now)
└─ Project                     one per client / app engagement
   ├─ Target                   the app's environments; one canonical per test
   ├─ TerritoryNotes           per-target routes, widget idioms, conventions, volatile
   │                           regions — recon-written, human-editable, versioned
   ├─ Credential → Session     named personas (admin, member, …), each with its own
   │                           seeded session + validity probe (§7)
   ├─ Module → ModuleVersion   shared step fragments (login, nav) with their own
   │                           lineage; referenced as module@version
   ├─ Test → TestVersion       the Bundle (JSONB steps), lineage, calibration,
   │                           state: draft → calibrated → frozen
   ├─ Suite → SuiteRun         saved sets (smoke, regression, per-feature) + selection-mode
   │  │                        runs; triggers: manual / API / deploy-hook — no cron in v1
   │  └─ RunResult             keyed (test, version, param_row, browser, viewport,
   │     │                     environment) — matrix-ready, constant in v1
   │     └─ StepEvent          one row per executed step, written live by the
   │                           interpreter; prunes with the run
   └─ Incident                 cluster_key, members, representative, change_brief,
                               ruling + ruling_note, budget_spent (§8)
```

- **Module heal semantics — pin and propagate.** Tests pin `module@version`. Module drift heals the module once (vN+1) and opens one incident; the ruling bumps every referencing test's pin. Login drift is one fix, never N.
- **Persona binding.** A test binds to exactly one credential; the pre-suite probe validates every credential's session the suite will need and parks on any expiry (§7). Credential secrets are encrypted at rest (per-deployment envelope key — the VAULT_KEY pattern).
- **Draft lifecycle.** Only frozen versions replay or count toward history; drafts may hold partially-resolved steps, so the future interactive editor is a UI over existing states, never a schema change.
- **Store.** All of the above is database-native — Postgres, JSONB for step bodies. File export and a per-project git mirror are features layered on top, never sources of truth.

---

## 15. Operational notes (post-freeze — deployment / as-built)

> Added post-freeze at the maintainer's request. These are operational caveats for whoever deploys
> and scales the service — **not** changes to the decision register (D1–D37), which remains frozen.
> The full as-built deploy/scale reference is `ARCHITECTURE.md`.

- **Evidence storage & horizontal scale (§12).** The v1 `ArtifactStore` is `LocalDiskStore` only — the
  MinIO/S3 impl behind the same Protocol is not built yet. A single instance needs only a persistent
  local volume for `ANNEAL_ARTIFACTS`. **Running more than one API replica requires a shared /
  network-mounted `ANNEAL_ARTIFACTS`** (evidence written by one replica must be readable by another);
  until the object-store swap lands, horizontal scale is gated on this. The DB coordination primitives
  (SKIP-LOCKED claims, advisory locks, the per-target launch-lock index) already make multi-replica
  *safe* — shared artifact storage is the remaining prerequisite.

- **Run durability on restart (§2, D31).** Suite runs execute on daemon threads *inside* the API
  process. Restarting or redeploying the API **terminates any in-flight runs** — the startup reaper
  marks them `parked / INTERRUPTED` (releasing the per-target launch lock) but does **not** resume
  them. Deploy when targets are idle, or expect to re-launch interrupted runs.

- **Retention prune wiring (§12, D12).** The 7-day `prune_runs` sweep is implemented as a library
  function but is **not wired to any scheduler / cron / CLI** — it must be scheduled by the deployment
  (e.g. a cron/k8s CronJob) or Postgres and the evidence disk grow unbounded. Note the prune removes
  DB rows + path columns; **disk-side artifact file deletion is not yet implemented** and needs a
  companion file GC on the artifacts volume.

---

*Sources: a comparative benchmark against a same-day product redesign (frozen replay red in 29s / live agent green in 252s / warm 127s). The Bundle schema is derived from `fixtures/campaign-crud.bundle.jsonc`, the normative example; where the fixture and this prose disagree, the fixture governs.*
