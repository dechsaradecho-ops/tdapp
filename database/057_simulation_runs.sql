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
-- evidence, not a journal — `log_maintenance` trims them. `simulation_runs`
-- keeps the verdict JSON so the summary survives even after the events go.

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
