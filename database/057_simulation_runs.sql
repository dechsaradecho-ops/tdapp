-- 057_simulation_runs.sql
-- Barrier-simulation runs + their per-event labels, so the Logs > Simulate
-- tab can show a progress bar while a run is in flight and a full, auditable
-- result afterwards.
--
-- WHY (owner 2026-10-06)
-- ----------------------
-- The platform had no way to answer "does this signal have an edge, and at
-- what stop width?". `sl_distance_min_pct` and `rr_target` were both picked
-- by reading 44 losing trades. Replaying the signal generator over daily
-- history answered it, but only from a CLI script whose numbers nobody could
-- reproduce or re-run.
--
-- So the study becomes a first-class, re-runnable feature:
--   simulation_runs   one row per run — status, progress, config, verdict
--   simulation_events one row per LABELLED EVENT — the actual sample
--
-- The events table is the point. A run's verdict is recomputable from it
-- (`SELECT avg(r_multiple) ...`), so a surprising number can always be traced
-- back to the individual trades that produced it instead of trusted.
--
-- RETENTION: like every other log table here these rows are disposable
-- evidence, not a journal — `simulation.purge_old_runs` (called by the
-- log_maintenance worker) trims them. `simulation_runs` keeps the verdict
-- JSON so the summary survives even after the events go.
--
-- A 5,000-event run writes 5,000 rows, and the owner can run several in a
-- row while tuning — without a purge these tables are the fastest-growing
-- thing in the database.

create table if not exists simulation_runs (
    id            text primary key,
    status        text not null default 'pending'
                  check (status in ('pending', 'running', 'done', 'failed', 'cancelled')),
    stage         text not null default 'queued',
    -- how many events the caller asked for vs how many were produced. The
    -- gap is meaningful: history is finite, so a run can come up short.
    target_events integer,
    total_events  integer not null default 0,
    processed     integer not null default 0,
    config        jsonb,
    result        jsonb,
    error         text,
    started_at    timestamptz,
    finished_at   timestamptz,
    created_at    timestamptz not null default now()
);

comment on table simulation_runs is
    'Barrier-simulation runs (Logs > Simulate). result holds the sweep, the '
    'walk-forward verdict, per-asset breakdown and MFE/MAE summary.';
comment on column simulation_runs.result is
    'Verdict JSON: grid rows, train/test split, per-asset, feature spread, MFE/MAE quantiles.';

create table if not exists simulation_events (
    id           text primary key,
    run_id       text not null references simulation_runs(id) on delete cascade,
    seq          integer not null,
    asset        text not null,
    direction    text not null,
    bar_index    integer,
    entry        numeric,
    atr_pct      numeric,
    opportunity  numeric,
    confidence   numeric,
    -- the barrier cell this label belongs to
    sl_mult      numeric,
    tp_r         numeric,
    max_bars     integer,
    -- the label itself
    label        text not null
                 check (label in ('tp', 'sl', 'expired', 'pending')),
    r_multiple   numeric,
    bars_held    integer,
    exit_price   numeric,
    ambiguous    boolean not null default false,
    gap_fill     boolean not null default false,
    mfe_r        numeric,
    mae_r        numeric,
    created_at   timestamptz not null default now()
);

create index if not exists simulation_events_run_seq_idx
    on simulation_events (run_id, seq);

-- The UI polls "give me everything after seq N" to stream the run live, so
-- the run_id + seq index has to cover that read.
create index if not exists simulation_events_run_idx
    on simulation_events (run_id);

create index if not exists simulation_runs_created_idx
    on simulation_runs (created_at desc);

-- ---------------------------------------------------------------------------
-- RLS — service_role ONLY.
--
-- Verified while writing this: the frontend has @supabase/supabase-js in
-- package.json but never imports it (no createClient anywhere in
-- frontend/src). Every read and write for these tables goes through the
-- backend API with the service key. So no browser-side path needs a policy,
-- and omitting one is the entire point of enabling RLS: with no policy,
-- anon and authenticated get nothing, while service_role bypasses RLS by
-- design.
--
-- Note the difference from migration 054, which grants `for all using(true)`
-- on settings_change_logs. That one is wider than this codebase's data flow
-- appears to need; these tables are deliberately tighter because the blast
-- radius is worse — these rows are the EVIDENCE behind every "does this
-- signal have an edge" claim, and a publicly writable table would let anyone
-- rewrite the trades a verdict was computed from.
-- ---------------------------------------------------------------------------
alter table simulation_runs enable row level security;
alter table simulation_events enable row level security;

drop policy if exists "simulation_runs_service_all" on simulation_runs;
create policy "simulation_runs_service_all"
    on simulation_runs for all
    to service_role
    using (true)
    with check (true);

drop policy if exists "simulation_events_service_all" on simulation_events;
create policy "simulation_events_service_all"
    on simulation_events for all
    to service_role
    using (true)
    with check (true);

-- Grants are explicit rather than relying on the default privileges Supabase
-- grants a new table to `anon`/`authenticated`. Belt and braces: even if a
-- future default changes, these roles hold nothing here.
revoke all on simulation_runs from anon, authenticated;
revoke all on simulation_events from anon, authenticated;
