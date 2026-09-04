-- ============================================================
-- Migration 012 — Per-asset Min Confidence for gold (XAUUSD)
-- Run in Supabase SQL Editor (supplements 001-011)
-- ============================================================

-- Gold gets its own signal-quality threshold, applied to BOTH signal
-- generation (market_scanner) and order opening (execution gate).
-- NULL = use the base min_confidence (backwards compatible with rows
-- created before this migration).
alter table trading_settings
  add column if not exists min_confidence_gold numeric(5, 2);
