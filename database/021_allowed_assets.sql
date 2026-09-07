-- ============================================================
-- Migration 021 — User-managed tradable universe (allowed_assets)
-- Run in Supabase SQL Editor (supplements 001-020)
-- ============================================================

-- List of assets the user wants the Market Scanner to analyse and the
-- platform to trade (JSONB array of symbols, e.g. ["EURUSD","XAUUSD"]).
-- The Settings UI only offers pairs from quotes.SUPPORTED_ASSETS — pairs
-- the price feeds (Yahoo → Frankfurter/TwelveData → exchangerate) cover.
-- NULL/missing → engines fall back to quotes.DEFAULT_ASSETS (the original
-- 5 assets), so existing rows keep current behaviour.
alter table trading_settings
  add column if not exists allowed_assets jsonb;

-- Normalize legacy rows: empty array → NULL (engine default kicks in).
update trading_settings
  set allowed_assets = null
  where allowed_assets is not null
    and jsonb_array_length(allowed_assets) = 0;
