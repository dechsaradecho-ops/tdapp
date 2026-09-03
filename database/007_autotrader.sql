-- ============================================================
-- Migration 007 — AutoTrader (Phase 1: paper auto-trading)
-- Run in Supabase SQL Editor (supplements 001-006)
-- ============================================================

-- ---------- 1. paper_trades: execution journal ----------
-- One row per order actually fired (auto OR approved semi-auto).
-- The auto trader reads this table for frequency limits; the position
-- guard writes close-outs (SL/TP hits) back to it.
create table if not exists paper_trades (
  id            uuid primary key default uuid_generate_v4(),
  user_id       text not null default 'demo',
  signal_id     text,
  asset         text not null,
  direction     text not null check (direction in ('BUY', 'SELL')),
  volume        numeric(12, 2) not null default 0,
  entry_price   numeric(18, 5),
  stop_loss     numeric(18, 5),
  take_profit   numeric(18, 5),
  exit_price    numeric(18, 5),
  pnl           numeric(18, 2),
  status        text not null default 'open' check (status in ('open', 'closed', 'rejected')),
  source        text not null default 'auto' check (source in ('auto', 'approved')),
  ticket        text,                       -- broker ticket (PAPER-xxxxxx)
  close_reason  text,                       -- 'sl' | 'tp' | 'manual'
  closed_at     timestamptz,
  created_at    timestamptz not null default now()
);
create index if not exists idx_paper_trades_user on paper_trades(user_id, created_at desc);
create index if not exists idx_paper_trades_open on paper_trades(status) where status = 'open';
create index if not exists idx_paper_trades_date on paper_trades(date(created_at));

-- ---------- 2. trading_pause: real kill-switch state ----------
-- Single row (id=1). Execution path MUST read this before firing orders.
create table if not exists trading_pause (
  id           int primary key default 1 check (id = 1),
  paused       boolean not null default false,
  reason       text not null default '',
  paused_at    timestamptz,
  updated_at   timestamptz not null default now()
);

alter table paper_trades enable row level security;
alter table trading_pause enable row level security;

create policy "read paper trades"       on paper_trades   for select using (true);
create policy "service write paper trades" on paper_trades
  for insert to service_role with check (true);
create policy "service update paper trades" on paper_trades
  for update to service_role using (true);
create policy "service delete paper trades" on paper_trades
  for delete to service_role using (true);

create policy "read trading pause"      on trading_pause  for select using (true);
create policy "service write trading pause" on trading_pause
  for insert to service_role with check (true);
create policy "service update trading pause" on trading_pause
  for update to service_role using (true);

insert into trading_pause (id) values (1) on conflict (id) do nothing;

-- ---------- 3. free order_mode: allow auto/semi_auto/manual ----------
-- 006 shipped with ('auto','market','limit','stop') but the Settings page and
-- the auto trader speak auto / semi_auto / manual (TradingMode enum).
alter table trading_settings drop constraint if exists trading_settings_order_mode_check;
alter table trading_settings add constraint trading_settings_order_mode_check
  check (order_mode in ('auto', 'semi_auto', 'manual'));

-- Migrate any old values; keep existing 'auto' rows as-is.
update trading_settings set order_mode = 'semi_auto' where order_mode in ('market', 'limit', 'stop');
