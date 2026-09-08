-- ============================================================
-- Migration 027 — Per-symbol paper spread (spread_overrides)
-- Run in Supabase SQL Editor (supplements 001-026)
-- ============================================================

-- Per-symbol spread overrides for paper fills (JSONB object mapping
-- asset → spread in price units, e.g. {"XAUUSD": 0.35, "EURUSD": 0.0001}).
-- Resolution order in the engine (effective_spread):
--   1. spread_overrides[ASSET]   (user override, this column)
--   2. DEFAULT_SPREADS[ASSET]    (built-in realistic typical spread)
--   3. paper_spread              (legacy global fallback, migration 017)
-- NULL/missing → built-in defaults only, so existing rows keep working.
alter table trading_settings
  add column if not exists spread_overrides jsonb;

-- Normalize legacy rows: empty object → NULL (built-in defaults kick in).
update trading_settings
  set spread_overrides = null
  where spread_overrides is not null
    and spread_overrides = '{}'::jsonb;
