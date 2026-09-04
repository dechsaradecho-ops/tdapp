-- ============================================================
-- Migration 013 — signal lifecycle log (7-day auto-expiry)
-- Run in Supabase SQL Editor (supplements 001-012)
--
-- WHY: the `signals` table only keeps the LATEST state per row
-- (pending → approved/rejected/expired). Once a signal fires or
-- expires, WHY it opened / did not open is lost. This table logs
-- every lifecycle event:
--
--   created        scanner generated the signal (with its reasons)
--   order_opened   order actually placed (ticket + volume)
--   order_blocked  gate said NO (pause/limits/news/correlation/...,
--                  broker rejection, bad sizing) — reason is stored
--   rejected       user pressed ปฏิเสธ (semi-auto)
--   expired        pending past the 30-min TTL
--   closed         position closed (SL/TP/manual) — exit price + PnL
--
-- RETENTION: rows older than 7 days are deleted by the backend
-- (signal_log.purge_old_logs — throttled, runs from the endpoint).
-- No cron needed. Safe to re-run (IF NOT EXISTS guards).
-- ============================================================

create table if not exists signal_logs (
  id            uuid primary key default uuid_generate_v4(),
  signal_id     text not null default '',        -- signals.id (text: no FK, tolerant)
  asset         text not null default '',
  direction     text not null default '',        -- buy | sell
  event         text not null default '',        -- created | order_opened | order_blocked | rejected | expired | closed
  confidence    numeric(6, 2),
  entry         numeric(18, 5),
  stop_loss     numeric(18, 5),
  take_profit   numeric(18, 5),
  source        text not null default '',        -- scanner | auto | approved
  reason        text not null default '',        -- WHY: opened / not opened / expired (Thai-readable)
  ticket        text not null default '',        -- PAPER-xxxx when the order opened
  volume        numeric(12, 3),
  pnl           numeric(18, 2),
  exit_price    numeric(18, 5),
  created_at    timestamptz not null default now()
);

create index if not exists idx_signal_logs_created
  on signal_logs(created_at desc);
create index if not exists idx_signal_logs_asset
  on signal_logs(asset, created_at desc);

alter table signal_logs enable row level security;
create policy "read signal logs" on signal_logs
  for select using (true);
create policy "write signal logs service" on signal_logs
  for insert to service_role with check (true);
create policy "delete signal logs service" on signal_logs
  for delete to service_role using (true);
