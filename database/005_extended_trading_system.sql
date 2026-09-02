-- ============================================================
-- Migration 005 — Extended Trading System tables
-- Run in Supabase SQL Editor (supplements 001/002)
-- ============================================================

-- AI Trading Journal — every trade decision recorded for 7/30/90d analysis
create table if not exists trading_journal (
  id                uuid primary key default uuid_generate_v4(),
  asset             text not null,
  direction         trade_direction not null,
  entry_price       numeric(18, 5) not null,
  exit_price        numeric(18, 5),
  holding_time_min  numeric(12, 2),
  pnl               numeric(18, 2),
  rr_ratio          numeric(8, 2),
  market_regime     text not null default '',
  opportunity_score numeric(5, 2) not null default 0,
  ai_explanation    text not null default '',
  trade_date        date not null default current_date,
  created_at        timestamptz not null default now()
);
create index if not exists idx_journal_date on trading_journal(trade_date desc);
create index if not exists idx_journal_asset on trading_journal(asset, created_at desc);

-- Economic Calendar — high-impact events gate new orders (30-min block)
create table if not exists economic_calendar (
  id          uuid primary key default uuid_generate_v4(),
  event       text not null,
  currency    text not null default 'USD',
  event_time  timestamptz not null,
  impact      text not null default 'high',  -- high / medium / low
  actual      text,
  forecast    text,
  previous    text,
  created_at  timestamptz not null default now()
);
create index if not exists idx_calendar_time on economic_calendar(event_time asc);

-- Kill Switch audit log — one row per engagement
create table if not exists kill_switch_log (
  id           uuid primary key default uuid_generate_v4(),
  triggers     jsonb not null default '[]'::jsonb,
  context      jsonb not null default '{}'::jsonb,
  engaged_at   timestamptz not null default now()
);
create index if not exists idx_killswitch_time on kill_switch_log(engaged_at desc);

-- Walk Forward / Backtest results cache
create table if not exists backtest_results (
  id                uuid primary key default uuid_generate_v4(),
  asset             text not null,
  indicator         text not null,
  days              int not null default 120,
  win_rate_pct      numeric(5, 2) not null default 0,
  profit_factor     numeric(8, 2) not null default 0,
  sharpe_ratio      numeric(8, 2) not null default 0,
  max_drawdown_pct  numeric(5, 2) not null default 0,
  reliability_score numeric(5, 2) not null default 0,
  result            jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now()
);
create index if not exists idx_backtest_asset on backtest_results(asset, created_at desc);

-- ---------- RLS: backend (service_role) writes, API reads ----------
alter table trading_journal   enable row level security;
alter table economic_calendar enable row level security;
alter table kill_switch_log   enable row level security;
alter table backtest_results  enable row level security;

create policy "read trading journal"     on trading_journal   for select using (true);
create policy "read economic calendar"   on economic_calendar for select using (true);
create policy "read kill switch log"     on kill_switch_log   for select using (true);
create policy "read backtest results"    on backtest_results  for select using (true);

create policy "service insert trading journal"   on trading_journal   for insert to service_role with check (true);
create policy "service insert economic calendar" on economic_calendar for insert to service_role with check (true);
create policy "service insert kill switch log"   on kill_switch_log   for insert to service_role with check (true);
create policy "service insert backtest results"  on backtest_results  for insert to service_role with check (true);

create policy "service update trading journal"  on trading_journal  for update to service_role using (true);
create policy "service delete trading journal"  on trading_journal  for delete to service_role using (true);
create policy "service delete economic calendar" on economic_calendar for delete to service_role using (true);
create policy "service delete kill switch log"   on kill_switch_log   for delete to service_role using (true);
create policy "service delete backtest results"  on backtest_results  for delete to service_role using (true);
