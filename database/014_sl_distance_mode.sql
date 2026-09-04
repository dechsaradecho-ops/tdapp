-- ============================================================
-- Migration 014 — SL/TP distance mode (สั้น / กลาง / ยาว)
-- Run in Supabase SQL Editor (supplements 001-013)
-- ============================================================

-- Signal cards preview SL/TP at 3 stop distances (สั้น ×1.0 / กลาง ×1.5 /
-- ยาว ×2.0 ATR). This setting decides which tier opens the REAL order.
-- Stored signal rows always carry the กลาง (×1.5) prices — the default —
-- and execute_signal re-derives SL/TP for the chosen tier at fire time.
alter table trading_settings
  add column if not exists sl_distance_mode text not null default 'medium'
    check (sl_distance_mode in ('short', 'medium', 'long'));
