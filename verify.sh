#!/usr/bin/env bash
# verify.sh — the deterministic build gate for Anneal (GOAL.md).
# DONE == this script exits 0 for the target milestone. The model's opinion does not count.
#
# Usage:  ./verify.sh [m0|m1|m2|m3|m4|all|holdout]   (default: all)
# `all` = m0-m4 + the typed API contract — engine mechanics, fully reproducible against the
# hermetic golden app, no live target and no credentials required.
# Each milestone gate also re-runs every lower milestone (regression sweep).
# Every run first enforces two frozen guards the agent cannot weaken:
#   check_frozen_bar     — the bar (baseline/schemas/fixture) is unmodified
#   check_target_agnostic — no app-specific literals in engine code (anti-overfit)
# Opt-in acceptance gate (bring your own target):
#   ANNEAL_HOLDOUT_DIR=/path ./verify.sh holdout        # unseen flows (anti-overfit)
# The frozen NL corpus lives in parity/corpus/*.nl.txt.
#
# Requires: docker (compose v2), uv. Brings up postgres + the golden app, runs
# migrations, then the milestone's structural checks + pytest markers.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
TARGET="${1:-all}"

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }
head() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# ── FROZEN-BAR integrity (the Karpathy 'prepare.py' invariant) ───────────────
# These files ARE the bar. The build agent may build the harness that RUNS the
# checks; it may NEVER weaken the checks or the bar to make a gate pass. This
# guard runs before every gate and refuses if any frozen file changed.
# If YOU (the human) intentionally change the bar, re-pin the hash below with:
#   shasum -a 256 schemas/*.json fixtures/campaign-crud.bundle.jsonc
check_frozen_bar() {
  head "frozen-bar integrity (un-gameable gate)"
  local ok=1
  _pin() {  # $1=expected-sha256  $2=path
    local got; got="$(shasum -a 256 "$2" 2>/dev/null | awk '{print $1}')" || true
    if [ "$got" != "$1" ]; then
      printf '  \033[31m✗ FROZEN FILE CHANGED: %s\033[0m\n    expected %s\n    got      %s\n' "$2" "$1" "${got:-<missing>}"
      ok=0
    fi
  }
  _pin 29e65cde23ea0bb116b44d3dd1e1beac7aa45a26c2c5732a5c48cb2ce1dbd580 schemas/bundle.schema.json
  _pin f75139de6029555c2667f8115b69371945ce092b8712beafa7ad1bbb9a09cdb9 schemas/step-event.schema.json
  _pin 4386d2f6d7a8032314c7ebd4c1ce1b2d3a49b466ba5a298760b4f861eeaaf87f fixtures/campaign-crud.bundle.jsonc
  [ "$ok" = 1 ] || fail "the bar was modified — editing it to pass a gate is the ONE unrecoverable failure (GOAL.md). Revert it; or if the change is intentional, re-pin the hash in verify.sh yourself."
  # semantic backstop (defends even a careless re-pin): P1-P5 present, N1 still must-fail.
  for _f in P1 P2 P3 P4 P5 N1; do
    ls parity/corpus/"$_f"-*.nl.txt >/dev/null 2>&1 \
        || fail "corpus integrity: parity/corpus/$_f-*.nl.txt is missing — P1-P5 + N1 are the frozen bar"
  done
  grep -q 'expect: RED' parity/corpus/N1-negative-oracle-honesty.nl.txt \
      || fail "N1 corpus flow no longer marked 'expect: RED' — oracle-honesty gate defeated"
  pass "frozen bar unchanged; corpus P1-P5 + N1(red) intact"
}

# ── TARGET-AGNOSTIC guard (anti-overfit, frozen) ─────────────────────────────
# The engine must contain NO app-specific literals — routes, testids, flow choreography, or
# few-shot prompt examples tied to a target. App specifics live in TargetProfile / TerritoryNotes
# DATA, never in engine code or prompts (that leak is exactly what overfits an engine to its demo
# app). This grep lives in verify.sh (frozen — the build agent cannot weaken it) and runs before
# every gate. It scans engine packages only; the mock target (fixtures/app) and tests legitimately
# name the app and are not scanned.
check_target_agnostic() {
  head "target-agnostic guard (anti-overfit)"
  local deny='campaign-v2|campaign-test-agent|input-max-concurrency|input-campaign|input-start-date|input-end-date|btn-(add|create|campaign)|select-outbound-agent|btn-back-to-campaigns|/campaign-v2'
  local hits=""
  for d in packages/*/src; do
    local h; h="$(grep -rniE "$deny" "$d" 2>/dev/null || true)"
    [ -n "$h" ] && hits="${hits}${h}"$'\n'
  done
  if [ -n "$hits" ]; then
    printf '%s\n' "$hits"
    fail "target-specific literal(s) in engine code — this overfits the engine to the demo app. App specifics belong in TargetProfile/TerritoryNotes DATA, not engine code or prompts."
  fi
  pass "no target-specific literals in any engine package"
}

# HARD RULE (S. Rathod): prompts must be TARGET-AGNOSTIC. The literal guard above catches a leaked
# string; this catches SEMANTIC overfit — a target domain noun smuggled into a prompt with no literal
# (e.g. "open the campaign's dropdown", "concurrency is an integer"). Prompts speak ONLY generic web
# semantics (element/role/option/href/field/list/dropdown). App knowledge lives in TargetProfile/
# TerritoryNotes DATA, never a prompt — not even as an inline example or a "don't hardcode X" comment.
check_prompts_agnostic() {
  head "prompt-agnostic guard (hard rule: no target nuance in prompts)"
  local nouns='campaign|crew|concurrency|outbound|inbound|dialler|voice.?agent'
  local files; files="$( { grep -rlE '_(SYSTEM|PROMPT)[[:space:]]*=' packages/*/src 2>/dev/null; \
                           find packages -path '*/src/*' \( -name prompts.py -o -name interview.py \) 2>/dev/null; \
                         } | sort -u )"
  local hits=""
  for f in $files; do
    [ -f "$f" ] || continue
    local h; h="$(grep -niE "$nouns" "$f" 2>/dev/null || true)"
    [ -n "$h" ] && hits="${hits}${f}:"$'\n'"${h}"$'\n'
  done
  if [ -n "$hits" ]; then
    printf '%s\n' "$hits"
    fail "target domain vocabulary in a prompt file — prompts MUST be target-agnostic (generic web semantics only). App-specific knowledge belongs in TargetProfile/TerritoryNotes DATA, never in a prompt, an inline example, or a counter-example comment."
  fi
  pass "no target domain vocabulary in any prompt string"
}

# ── shared bring-up (idempotent) ─────────────────────────────────────────────
bring_up() {
  head "bring-up"
  command -v uv     >/dev/null || fail "uv not installed"
  command -v docker >/dev/null || fail "docker not installed"
  uv sync --frozen                                   || fail "uv sync"
  docker compose up -d postgres                      || fail "compose up postgres"
  # golden-app is force-recreated every gate: its in-memory CAMPAIGNS accumulates and a long-lived
  # process degrades (slow renders → false cold-replay LOCATOR_DRIFT reds after ~1 day uptime), so a
  # reused container makes the gate measure a decaying fixture, not the engine. Fresh process each run
  # + --wait on the healthcheck = the gate is deterministic w.r.t. the target.
  docker compose up -d --force-recreate --wait golden-app || fail "fresh golden-app"
  # wait for postgres
  for _ in $(seq 1 30); do
    docker compose exec -T postgres pg_isready -q && break || sleep 1
  done
  docker compose exec -T postgres pg_isready -q      || fail "postgres not ready"
  uv run alembic upgrade head                        || fail "migrations"
  pass "stack up + migrated"
}

# ── M0 — contracts ───────────────────────────────────────────────────────────
gate_m0() {
  head "M0 — contracts"

  # 1. both JSON schemas are themselves valid, and the fixture validates against the bundle schema.
  uv run python -m anneal_core.validate fixtures/campaign-crud.bundle.jsonc \
      || fail "fixture does not validate against schemas/bundle.schema.json"
  pass "fixture validates against bundle schema"

  # 2. D31 dependency rule: anneal-executor must NOT pull in anneal-llm (auditable, not reviewed).
  if uv tree --package anneal-executor 2>/dev/null | grep -q 'anneal-llm'; then
      fail "anneal-executor depends on anneal-llm — violates D31 (cold has no LLM in its dep graph)"
  fi
  if grep -rEn 'import[[:space:]]+anneal_llm|from[[:space:]]+anneal_llm' packages/anneal-executor/src >/dev/null 2>&1; then
      fail "anneal-executor imports anneal_llm — violates D31"
  fi
  pass "cold executor has no LLM in its dependency graph (D31)"

  # 3. target-agnostic grep audit + vendored COPY files import cleanly.
  uv run pytest -q -m m0_audit          || fail "target-agnostic audit / vendored-import checks (m0_audit)"
  pass "target-agnostic audit + vendored imports"

  # 4. THE GOLDEN TEST (GOAL.md / HLD §15): the fixture compiles to a step plan and replays
  #    green against the hermetic golden app, with postcondition waits, trace + video captured,
  #    and `anneal export` prints the matching standalone Playwright script.
  uv run pytest -q -m m0_golden         || fail "golden test: fixture compile + replay green + export (m0_golden)"
  pass "golden: fixture compiles + replays green + anneal export matches"
}

gate_m1() { head "M1 — cold lane + console v0"; uv run pytest -q -m m1 || fail "M1 (m1): read-only parallel suite / session probe park / evidence / retention / console v0"; pass "M1"; }
gate_m2() { head "M2 — hot lane";             uv run pytest -q -m m2 || fail "M2 (m2): NL author + calibrate + replay-twice-zero-residue / interview blocks / destructive guard"; pass "M2"; }
gate_m3() { head "M3 — red path";             uv run pytest -q -m m3 || fail "M3 (m3): heal-as-changelog / fallback re-freeze incident / redesign→one incident→ruling propagate|rollback / budgets / oscillation"; pass "M3"; }
gate_m4() { head "M4 — scale";                uv run pytest -q -m m4 || fail "M4 (m4): scheduler exclusive-lock + sweeper / ENV park / change-driven selection with reasons"; pass "M4"; }
gate_api() { head "A0 — typed API contract";  uv run pytest -q -m api_contract || fail "A0 (api_contract): OpenAPI describes the full surface / typed bodies+responses / legacy video removed (docs/BACKEND-SPEC.md)"; pass "A0"; }

# ── HOLDOUT — generalization on flows the agent NEVER saw (anti-overfit acceptance) ──
# Run by the HUMAN at milestone acceptance, NOT in the build loop. The flows live OUTSIDE the repo
# ($ANNEAL_HOLDOUT_DIR) so the build agent cannot read or optimize to them. The build implements
# the `holdout` marker: author each external NL flow through the hot lane and assert it replays to
# its expected verdict (green/red per the dir's holdout.json). Passing flows the engine never
# trained on is the proof of generality; an engine overfit to the corpus (P1-P5/N1) fails here.
gate_holdout() {
  head "HOLDOUT — generalization on unseen flows"
  [ -n "${ANNEAL_HOLDOUT_DIR:-}" ] || fail "holdout requires \$ANNEAL_HOLDOUT_DIR — a dir OUTSIDE this repo, populated by the human with flows the agent never saw"
  [ -d "$ANNEAL_HOLDOUT_DIR" ] || fail "\$ANNEAL_HOLDOUT_DIR ($ANNEAL_HOLDOUT_DIR) is not a directory"
  local n; n="$(find "$ANNEAL_HOLDOUT_DIR" -name '*.nl.txt' 2>/dev/null | wc -l | tr -d ' ')"
  [ "${n:-0}" -ge 3 ] || fail "holdout dir has ${n:-0} flows; need >=3 unseen flows to be meaningful (no vacuous pass)"
  ANNEAL_HOLDOUT_DIR="$ANNEAL_HOLDOUT_DIR" uv run pytest -q -m holdout \
      || fail "holdout: engine failed to author+replay an unseen flow to its expected verdict — GENERALIZATION FAILURE (overfit to the corpus). Flows: $ANNEAL_HOLDOUT_DIR"
  pass "engine generalized: $n unseen flows authored + replayed to expected verdicts"
}

run_upto() {  # run every gate through $1 as a regression sweep
  case "$1" in
    m0) gate_m0 ;;
    m1) gate_m0; gate_m1 ;;
    m2) gate_m0; gate_m1; gate_m2 ;;
    m3) gate_m0; gate_m1; gate_m2; gate_m3 ;;
    m4) gate_m0; gate_m1; gate_m2; gate_m3; gate_m4 ;;
    all) gate_m0; gate_m1; gate_m2; gate_m3; gate_m4; gate_api ;;
    holdout)     gate_holdout ;;
    *) fail "unknown target '$1' (expected m0|m1|m2|m3|m4|all|holdout)" ;;
  esac
}

bring_up
check_frozen_bar
check_target_agnostic
check_prompts_agnostic
run_upto "$TARGET"
head "RESULT"; pass "verify.sh $TARGET — all gates green"
