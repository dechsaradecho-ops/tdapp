-- 064_sim_opt_run_events.sql
-- Per-run optimizer event log (Task 3).
--
-- WHY
-- ---
-- A full optimization searches up to 500 candidates on a background worker,
-- so the UI cannot watch progress without a persisted, pollable event
-- stream. One row per event, ordered by a per-run monotonic ``seq`` the
-- worker assigns (never a wall-clock timestamp, which workers and clocks
-- cannot keep monotonic). The Logs page polls
-- ``GET /api/system/optimize/{id}/logs`` with ``offset=last_seq+1``.
--
-- Event logging is best-effort: the worker never fails a run because an
-- event row did not land. ``data`` carries small summaries only (PnL,
-- counts, hashes) — never secrets, raw exception dumps, or full configs.
--
-- Idempotent: safe to re-run (`if not exists` throughout); never touches
-- existing rows or tables.

-- ---------------------------------------------------------------------------
-- Events: one row per (run, seq) with level/stage/iteration context.
-- ---------------------------------------------------------------------------
create table if not exists sim_opt_run_events (
    run_id      text not null references sim_opt_runs (id) on delete cascade,
    seq         integer not null,
    level       text not null default 'info'
        check (level in ('info', 'warning', 'error')),
    stage       text,
    iteration   integer,
    event       text not null,
    message     text,
    data        jsonb,
    created_at  timestamptz not null default now(),
    primary key (run_id, seq)
);

create index if not exists sim_opt_run_events_run_seq_idx
    on sim_opt_run_events (run_id, seq);

comment on table sim_opt_run_events is
    'Optimizer per-run event log: pollable progress stream ordered by (run_id, seq).';

-- ---------------------------------------------------------------------------
-- RLS — service_role ONLY (same posture as migrations 057/058/062/063).
--
-- The frontend never creates a Supabase client — every read/write goes
-- through the backend API with the service key — so no browser-side path
-- needs a policy. With RLS enabled and no policy for them, anon and
-- authenticated get nothing; service_role bypasses RLS by design.
-- ---------------------------------------------------------------------------
alter table sim_opt_run_events enable row level security;

drop policy if exists "sim_opt_run_events_service_all" on sim_opt_run_events;
create policy "sim_opt_run_events_service_all"
    on sim_opt_run_events for all
    to service_role
    using (true)
    with check (true);

revoke all on sim_opt_run_events from anon, authenticated;
