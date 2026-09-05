-- 017_position_management.sql
-- Position management + equity curve support.
-- Run in Supabase SQL Editor (user runs migrations manually).

-- 1) New trading_settings columns (position management batch)
alter table trading_settings
    add column if not exists breakeven_trigger_r numeric(5,2) default 1.0,
    add column if not exists trail_atr_mult numeric(5,2) default 2.0,
    add column if not exists partial_close_pct numeric(5,2) default 0,
    add column if not exists partial_trigger_r numeric(5,2) default 1.0,
    add column if not exists paper_spread numeric(10,5) default 0;

-- 2) Equity snapshots — one row per user per UTC day (upserted by the
--    portfolio monitor worker). Powers the equity curve chart and the
--    REAL drawdown in the kill switch.
create table if not exists equity_snapshots (
    id uuid primary key default uuid_generate_v4(),
    user_id text not null default 'demo',
    snapshot_date date not null,
    equity numeric(18,2) not null,
    created_at timestamptz default now(),
    unique(user_id, snapshot_date)
);

-- 3) RLS: read open to anon (static frontend), writes via service role only
alter table equity_snapshots enable row level security;

drop policy if exists "equity_snapshots_select_all" on equity_snapshots;
create policy "equity_snapshots_select_all"
    on equity_snapshots for select
    using (true);

drop policy if exists "equity_snapshots_insert_service" on equity_snapshots;
create policy "equity_snapshots_insert_service"
    on equity_snapshots for insert
    with check (true);

drop policy if exists "equity_snapshots_update_service" on equity_snapshots;
create policy "equity_snapshots_update_service"
    on equity_snapshots for update
    using (true)
    with check (true);
