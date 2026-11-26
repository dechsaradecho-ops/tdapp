-- 063_sim_optimizer.sql
-- Sandbox optimizer runs, iterations, trades, and equity curves.
--
-- WHY
-- ---
-- The optimizer searches candidate AppSettings over a pinned V2 price
-- dataset (Plan 2 replay engine) using a chronological 70/30
-- train/validation split. Every run pins its seed, starting capital,
-- search config, baseline settings, dataset version, and data hash so a
-- recommendation can be re-derived exactly. Every iteration stores its
-- full config, diff, and summary metrics; full trades/equity are stored
-- for the baseline, the current best, and the final top five.
--
-- Objective (binding): highest net simulated PnL; drawdown is reported,
-- not penalized. Realistic costs only in phase one.
--
-- Idempotent: safe to re-run (`if not exists` throughout); never touches
-- existing rows.

-- ---------------------------------------------------------------------------
-- Runs: one row per optimization run.
-- ---------------------------------------------------------------------------
create table if not exists sim_opt_runs (
    id                text primary key,
    status            text not null default 'pending'
        check (status in ('pending', 'running', 'complete', 'failed', 'cancelled')),
    stage             text,
    progress          numeric,
    seed              integer not null default 0,
    starting_capital  numeric not null default 1000,
    search_config     jsonb,
    baseline_settings jsonb,
    dataset_version   text,
    data_hash         text,
    result            jsonb,
    error             text,
    created_at        timestamptz not null default now(),
    started_at        timestamptz,
    finished_at       timestamptz
);

create index if not exists sim_opt_runs_status_created_idx
    on sim_opt_runs (status, created_at desc);

comment on table sim_opt_runs is
    'Sandbox optimizer runs: seed, capital, search config, pinned dataset, result.';

-- ---------------------------------------------------------------------------
-- Iterations: one row per (run, iteration) with full config + summary metrics.
-- ---------------------------------------------------------------------------
create table if not exists sim_opt_iterations (
    run_id             text not null references sim_opt_runs (id) on delete cascade,
    iteration          integer not null,
    config             jsonb,
    config_diff        jsonb,
    train_metrics      jsonb,
    validation_metrics jsonb,
    improved           boolean not null default false,
    created_at         timestamptz not null default now(),
    primary key (run_id, iteration)
);

create index if not exists sim_opt_iterations_run_iter_idx
    on sim_opt_iterations (run_id, iteration);

comment on table sim_opt_iterations is
    'Optimizer iterations: full candidate config, diff vs baseline, train/validation summary metrics.';

-- ---------------------------------------------------------------------------
-- Trades: full replay trades for stored iterations
-- (baseline, current best, final top five).
-- ---------------------------------------------------------------------------
create table if not exists sim_opt_trades (
    run_id      text not null references sim_opt_runs (id) on delete cascade,
    iteration   integer not null,
    trade_id    text not null,
    asset       text not null,
    direction   text not null check (direction in ('long', 'short')),
    entry_date  date not null,
    entry_price numeric not null,
    exit_date   date,
    exit_price  numeric,
    volume      numeric,
    pnl_usd     numeric,
    r_multiple  numeric,
    exit_reason text,
    mae_r       numeric,
    mfe_r       numeric,
    ambiguous   boolean not null default false,
    primary key (run_id, iteration, trade_id)
);

create index if not exists sim_opt_trades_run_iter_idx
    on sim_opt_trades (run_id, iteration);

comment on table sim_opt_trades is
    'Optimizer replay trades for stored iterations (baseline, best, final top five).';

-- ---------------------------------------------------------------------------
-- Equity: per-date equity curve for stored iterations.
-- ---------------------------------------------------------------------------
create table if not exists sim_opt_equity (
    run_id        text not null references sim_opt_runs (id) on delete cascade,
    iteration     integer not null,
    date          date not null,
    equity        numeric not null,
    realized_pnl  numeric,
    unrealized_pnl numeric,
    drawdown_pct  numeric,
    primary key (run_id, iteration, date)
);

create index if not exists sim_opt_equity_run_iter_idx
    on sim_opt_equity (run_id, iteration);
create index if not exists sim_opt_equity_run_date_idx
    on sim_opt_equity (run_id, date);

comment on table sim_opt_equity is
    'Optimizer equity curves for stored iterations (baseline, best, final top five).';

-- ---------------------------------------------------------------------------
-- RLS — service_role ONLY (same posture as migrations 057/058/062).
--
-- The frontend never creates a Supabase client — every read/write goes
-- through the backend API with the service key — so no browser-side path
-- needs a policy. With RLS enabled and no policy for them, anon and
-- authenticated get nothing; service_role bypasses RLS by design.
-- ---------------------------------------------------------------------------
alter table sim_opt_runs enable row level security;
alter table sim_opt_iterations enable row level security;
alter table sim_opt_trades enable row level security;
alter table sim_opt_equity enable row level security;

drop policy if exists "sim_opt_runs_service_all" on sim_opt_runs;
create policy "sim_opt_runs_service_all"
    on sim_opt_runs for all
    to service_role
    using (true)
    with check (true);

drop policy if exists "sim_opt_iterations_service_all" on sim_opt_iterations;
create policy "sim_opt_iterations_service_all"
    on sim_opt_iterations for all
    to service_role
    using (true)
    with check (true);

drop policy if exists "sim_opt_trades_service_all" on sim_opt_trades;
create policy "sim_opt_trades_service_all"
    on sim_opt_trades for all
    to service_role
    using (true)
    with check (true);

drop policy if exists "sim_opt_equity_service_all" on sim_opt_equity;
create policy "sim_opt_equity_service_all"
    on sim_opt_equity for all
    to service_role
    using (true)
    with check (true);

revoke all on sim_opt_runs from anon, authenticated;
revoke all on sim_opt_iterations from anon, authenticated;
revoke all on sim_opt_trades from anon, authenticated;
revoke all on sim_opt_equity from anon, authenticated;
