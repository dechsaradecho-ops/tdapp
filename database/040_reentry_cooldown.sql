-- 040_reentry_cooldown.sql
--
-- Re-entry cooldown กันเปิดซ้ำทันทีหลังปิดไม้ (1-minute close→reopen loop):
-- guard ปิดไม้แล้ว auto-trader ยิงสัญญาณ pending เดิมซ้ำในนาทีถัดไป (TTL 30 นาที)
-- ฟิลด์นี้คือจำนวนนาทีที่ต้องรอหลัง paper_trades.closed_at ของคู่เงินเดียวกัน
-- ก่อนเปิดออเดอร์ใหม่คู่นั้นได้ (0 = ปิดฟีเจอร์)
--
-- Idempotent: safe to re-run, and optional — every read path falls back to the
-- AppSettings default (30) so an un-applied migration keeps today's behaviour.
-- Settings save tolerates PGRST204 (drops unknown column with a warning).

alter table trading_settings
    add column if not exists reentry_cooldown_min integer not null default 30;

comment on column trading_settings.reentry_cooldown_min is
    'Cooldown กันเปิดซ้ำ (นาที): หลังปิดไม้คู่ไหน ต้องรอครบเท่านี้ก่อนเปิดคู่นั้นใหม่ — 0 = ปิดฟีเจอร์ (default 30)';
