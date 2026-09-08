-- ============================================================
-- Migration 025 — SL distance clamp (ระยะ SL เท่ากันทุกสัญลักษณ์)
-- Run in Supabase SQL Editor (supplements 001-024)
-- ============================================================

-- SL was price × ATR% × 1.5 per asset. Close-only FX feeds (Frankfurter)
-- have no intraday wicks → ATR understated → SL too tight, while OHLC feeds
-- (gold) don't — SL distances drifted 0.62%→1.17% of price across pairs.
-- The clamp pins the stop inside a % of price band:
--   0 disables a bound; set BOTH to the same value (e.g. 0.8 / 0.8)
--   to force a fixed SL distance on every asset.
alter table trading_settings
  add column if not exists sl_distance_min_pct numeric(6, 3) not null default 0;
alter table trading_settings
  add column if not exists sl_distance_max_pct numeric(6, 3) not null default 0;
