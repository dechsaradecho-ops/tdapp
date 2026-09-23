-- Migration 046 — SL-move notifications: own type + own cooldown, and a
-- truthful `channel` value for Web Push.
--
-- WHY (prod audit 2026-09-23, 240 notification rows):
--   1. `stop_loss` was reused for TRAILING SL MOVES (95 of 119 rows = 80%),
--      so the "Stop Loss" statistic was ~5x inflated and a user who turned
--      the stop_loss switch off also lost every SL-move alert. Same class as
--      the signal_logs `order_opened`-reused-for-SL-moves bug (commit 4d610ee).
--      → new enum value `sl_moved` (its own type, its own cooldown).
--   2. SL moves repeated every ~1 minute for the same asset (79 same-asset
--      pairs < 10 min apart; GBPCHF/EURCHF 7x in a row) — LINE spam.
--      → the service now throttles sl_moved per asset (SL_MOVE_COOLDOWN_MIN).
--   3. `channel` was hardcoded 'line' even when Web Push delivered the alert,
--      so the column never reflected reality. The enum had no web_push value.
--      → add `web_push` to notification_channel.
--
-- Idempotent: `add value if not exists` is safe to re-run.

-- ---------- notification_type: add sl_moved ----------
alter type notification_type add value if not exists 'sl_moved';

-- ---------- notification_channel: add web_push + both ----------
-- 'both' = LINE AND Web Push delivered the same alert (the normal case when
-- the user has a LINE target and a registered device).
alter type notification_channel add value if not exists 'web_push';
alter type notification_channel add value if not exists 'both';
