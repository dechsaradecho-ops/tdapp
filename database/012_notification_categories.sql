-- ============================================================
-- Migration 012 — LINE notification category toggles
-- Run in Supabase SQL Editor (supplements 001-011)
-- ============================================================

-- Per-category LINE notification switches stored on the single
-- trading_settings row (id=1). NotificationService.notify and
-- notification_worker.dispatch_pending check these before queueing/sending.
-- All default true so existing rows keep current behaviour.
alter table trading_settings
  add column if not exists notify_trade_opened  boolean not null default true,
  add column if not exists notify_trade_closed  boolean not null default true,
  add column if not exists notify_stop_loss     boolean not null default true,
  add column if not exists notify_risk_warning  boolean not null default true,
  add column if not exists notify_daily_digest  boolean not null default true,
  add column if not exists notify_daily_summary boolean not null default true;

-- dispatch_pending marks category-disabled rows as 'skipped' so they don't
-- get retried forever; needs a wider enum + an error note column.
alter type notification_status add value if not exists 'skipped';
alter table notifications add column if not exists error text;