-- ============================================================
-- Migration 006 — User-configurable settings (single-row table)
-- Run in Supabase SQL Editor (supplements 001-005)
-- ============================================================

-- One row holds the whole trading configuration; backend engines read it live.
create table if not exists trading_settings (
  id                        int primary key default 1 check (id = 1),

  -- profile + portfolio
  risk_profile              text not null default 'moderate'
                            check (risk_profile in ('conservative', 'moderate', 'aggressive')),
  capital                   numeric(18, 2) not null default 10000,
  min_confidence            numeric(5, 2) not null default 70,
  min_opportunity           numeric(5, 2) not null default 60,

  -- frequency limits (per profile defaults live in backend; these override)
  max_trades_daily          int not null default 6,
  max_trades_weekly         int not null default 30,
  max_open_positions        int not null default 4,
  risk_per_trade_pct        numeric(5, 2) not null default 1.0,

  -- risk / kill switch
  max_drawdown_pct          numeric(5, 2) not null default 10.0,
  kill_daily_loss_pct       numeric(5, 2) not null default 2.0,
  kill_weekly_loss_pct      numeric(5, 2) not null default 5.0,
  kill_monthly_loss_pct     numeric(5, 2) not null default 8.0,
  drawdown_throttle_pct     numeric(5, 2) not null default 5.0,

  -- news / correlation / order strategy
  news_block_minutes        int not null default 30,
  news_caution_minutes      int not null default 120,
  correlation_cap           numeric(5, 2) not null default 80,
  order_mode                text not null default 'auto'
                            check (order_mode in ('auto', 'market', 'limit', 'stop')),
  default_equity            numeric(18, 2) not null default 10000,
  paper_virtual_capital     numeric(18, 2) not null default 100000,

  -- backtest defaults
  backtest_days             int not null default 120,
  backtest_indicator        text not null default 'EMA',
  backtest_asset            text not null default 'EURUSD',

  updated_at                timestamptz not null default now()
);

-- ---------- RLS: backend (service_role) writes, API reads ----------
alter table trading_settings enable row level security;

create policy "read app settings" on trading_settings for select using (true);
create policy "service write app settings" on trading_settings
  for insert to service_role with check (true);
create policy "service update app settings" on trading_settings
  for update to service_role using (true);

-- Seed the single settings row
insert into trading_settings (id) values (1) on conflict (id) do nothing;
