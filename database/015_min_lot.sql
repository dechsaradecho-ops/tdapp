-- ============================================================
-- Migration 015 — Configurable minimum lot size (min_lot)
-- Run in Supabase SQL Editor (supplements 001-014)
-- ============================================================

-- Every opened order is sized by risk_to_lot (risk% of capital ÷ SL
-- distance), then floored at min_lot so tiny accounts still trade a
-- visible size. Default 0.01 keeps the current behaviour; raise it
-- (e.g. 0.02) from the Settings page.
alter table trading_settings
  add column if not exists min_lot numeric(6, 2) not null default 0.01;
