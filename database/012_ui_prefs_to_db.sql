-- ============================================================
-- Migration 012 — UI preferences moved out of localStorage
-- Run in Supabase SQL Editor (supplements 001-011)
-- ============================================================
-- The monitor / signals auto-refresh intervals used to live in the browser's
-- localStorage (tdapp_monitor_autorefresh / tdapp_signals_autorefresh) so the
-- choice was lost on every new device. They are now columns on the single
-- trading_settings row — same place as the rest of the app configuration.
--
-- Safe to re-run (IF NOT EXISTS guards).

alter table trading_settings
  add column if not exists monitor_refresh_sec int not null default 10;
alter table trading_settings
  add column if not exists signals_refresh_sec int not null default 0;
