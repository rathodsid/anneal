"""The §3 DDL as raw SQL — the single source the Alembic migration executes.

Shipped verbatim from LLD §3 so the DDL cannot skew from the contract. The concurrency-correctness
objects (M0's row in HLD §15) ship here, not discovered in M3: the open-incident partial unique index
(3.1a), the atomic budget guards (3.1b), the exclusive advisory lock (3.1c, a runtime call), and
SKIP-LOCKED claiming (3.1d, a runtime query). v1 ships the DDL as raw SQL; ORM models + repositories
land in M1 when the cold lane needs to read/write rows.
"""
from __future__ import annotations

DDL = r"""
-- gen_random_uuid() is core since PostgreSQL 13 — no pgcrypto extension needed (and managed PGs
-- like Azure Database for PostgreSQL don't allow-list pgcrypto by default). The vault's crypto is
-- Python-side Fernet, not DB-side, so no other pgcrypto function is used.

-- ── org is implicit/single (D17); project is the top real key. ──
create table project (
  id          text primary key,
  name        text not null,
  created_at  timestamptz not null default now()
);

create table target (
  id           uuid primary key default gen_random_uuid(),
  project_id   text not null references project(id),
  key          text not null,
  base_url     text not null,
  timezone     text not null,
  date_format  text not null default '%Y-%m-%d',
  max_parallel int  not null default 4,
  profile      jsonb not null default '{}',
  unique (project_id, key)
);

create table territory_notes (
  id          uuid primary key default gen_random_uuid(),
  target_id   uuid not null references target(id),
  version     int  not null,
  notes       jsonb not null,
  stale       boolean not null default false,
  authored_by text not null,
  created_at  timestamptz not null default now(),
  unique (target_id, version)
);

create table credential (
  id           uuid primary key default gen_random_uuid(),
  project_id   text not null references project(id),
  persona      text not null,
  secret_enc   bytea not null,
  unique (project_id, persona)
);

create table session (
  credential_id      uuid primary key references credential(id),
  storage_state_path text,
  seeded_at          timestamptz,
  last_validated     timestamptz,
  state              text not null default 'unknown'
);

create table module (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  ref text not null,
  head_version int,
  unique (project_id, ref)
);
create table module_version (
  id uuid primary key default gen_random_uuid(),
  module_id uuid not null references module(id),
  version int not null,
  steps jsonb not null,
  lineage jsonb not null,
  state text not null default 'frozen',
  created_at timestamptz not null default now(),
  unique (module_id, version)
);

create table test (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  test_key text not null,
  head_version int,
  quarantined boolean not null default false,
  -- free-form operator labels (a suite you can run without maintaining a membership array). CATALOG
  -- metadata, deliberately NOT in the bundle: relabelling is not a behaviour change, so it must not
  -- mint a version. Vocabulary is data — the engine validates shape only (rule 2).
  labels text[] not null default '{}',
  unique (project_id, test_key)
);

-- B4: the per-test GOLDEN reference recording — the human source-of-truth "how this test should look",
-- complementing the machine oracle. One per test (replace overwrites). Auto-seeded at freeze from a clean
-- cold replay of the frozen bundle; a reviewer upload overrides it. Bytes live in the artifact store.
create table test_golden (
  test_id          uuid primary key references test(id),
  video_path       text not null,
  source           text not null,              -- auto (clean freeze replay) | manual (reviewer upload)
  reflects_version int,                         -- the test_version the golden depicts
  created_at       timestamptz not null default now()
);

create table incident (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  state text not null default 'open',
  klass text not null,
  ck_surface     text not null,
  ck_step_sig    text not null,
  ck_error_class text not null,
  representative_test_id uuid references test(id),
  change_brief text,
  rep_concluded boolean not null default false,  -- discoverer finished (brief or not): unblocks guided waiters (§9.4)
  ruling text,
  ruling_note text,
  ruled_by text, ruled_at timestamptz,
  budget_agent_runs int not null default 0,
  budget_llm_calls  int not null default 0,
  cap_agent_runs int not null,
  cap_llm_calls  int not null,
  created_at timestamptz not null default now(),
  closed_at timestamptz
);
-- §9 ATTACH RACE: N parallel reds with the same cluster key converge to ONE open incident.
create unique index incident_open_cluster on incident (project_id, ck_surface, ck_step_sig, ck_error_class)
  where state in ('open','propagating');

create table test_version (
  id uuid primary key default gen_random_uuid(),
  test_id uuid not null references test(id),
  version int not null,
  bundle jsonb not null,
  state text not null default 'draft',
  reason text not null,
  parent_version int,
  ruling text,
  incident_id uuid references incident(id),
  triggered_by text,
  verified_against text not null,
  calibration jsonb,
  created_at timestamptz not null default now(),
  unique (test_id, version)
);

create table incident_member (
  incident_id uuid not null references incident(id),
  test_id uuid not null references test(id),
  red_run_id uuid,
  verify_run_id uuid,                            -- B3: the verify replay that proved the provisional (money-shot evidence)
  applied_version int,
  ruling text,                                   -- A5: the per-member decision (feature|bug)
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
  suite_id uuid references suite(id),
  mode text not null,
  trigger text not null,
  target_id uuid not null references target(id),
  state text not null default 'pending',
  verdict text,
  park_reason text,
  created_at timestamptz not null default now()
);
-- B1 LAUNCH LOCK: one active suite_run per target — the INSERT itself is the lock (no
-- check-then-insert TOCTOU). A second launch collides here and the API returns 409 with the
-- active id. Cleared by terminal states ('done'/'parked') and by the startup reaper.
create unique index one_active_suite_run_per_target on suite_run (target_id)
  where state in ('pending','probing','running');

-- ── the scheduler's claim unit: one row per (suite_run × test). Token minted on claim. ──
create table run_task (
  id uuid primary key default gen_random_uuid(),
  suite_run_id uuid not null references suite_run(id),
  test_id uuid not null references test(id),
  test_version int not null,
  exclusive boolean not null default false,
  state text not null default 'queued',
  token text,
  worker_id text,
  claimed_at timestamptz,
  unique (suite_run_id, test_id),
  unique (token)
);

create table run_result (
  id uuid primary key default gen_random_uuid(),
  run_task_id uuid not null references run_task(id),
  test_id uuid not null references test(id),
  test_version int not null,
  role text not null default 'primary',          -- B3: primary|retry|heal|verify (child runs share the parent's run_task)
  param_row text not null default 'default',
  browser text not null default 'chromium',
  viewport text not null default '1280x720',
  environment text not null,
  verdict text not null,
  error_class text,
  incident_id uuid references incident(id),
  trace_path text, video_path text, network_log_path text, screenshot_path text,
  pinned boolean not null default false,
  off_canonical boolean not null default false,
  duration_ms int, started_at timestamptz, finished_at timestamptz
);
alter table incident_member add constraint fk_member_run foreign key (red_run_id) references run_result(id);
alter table incident_member add constraint fk_member_verify_run foreign key (verify_run_id) references run_result(id);

create table step_event (
  id bigserial primary key,
  run_result_id uuid not null references run_result(id),
  step_index int not null,
  step_id text not null,
  intent text,
  action text not null,
  rung_used text,
  status text not null,
  error_class text,
  budget_ms int, elapsed_ms int,
  video_offset_ms int,
  network jsonb,
  detail jsonb,
  created_at timestamptz not null default now()
);
create index step_event_run_idx on step_event (run_result_id, step_index);

create table changelog (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references project(id),
  test_id uuid, incident_id uuid,
  kind text not null,
  summary text not null,
  ruling_note text,
  created_at timestamptz not null default now()
);
"""

# down: drop everything the migration created (M0 keeps this coarse; full public schema reset).
DROP = r"""
drop table if exists changelog, step_event, run_result, run_task, suite_run, suite,
  incident_member, test_version, incident, test, module_version, module, session,
  credential, territory_notes, target, project cascade;
"""
