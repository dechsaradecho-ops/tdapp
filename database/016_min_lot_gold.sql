-- ============================================================
-- Migration 016 — Per-asset Min Lot for gold (XAUUSD)
-- Run in Supabase SQL Editor (supplements 001-015)
-- ============================================================

-- Gold gets its own minimum lot floor, applied to position sizing
-- (execution.size_position). NULL = use the base min_lot (backwards
-- compatible with rows created before this migration).
alter table trading_settings
  add column if not exists min_lot_gold numeric(6, 2);
