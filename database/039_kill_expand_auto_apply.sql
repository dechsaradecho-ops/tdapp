-- 039_kill_expand_auto_apply.sql
--
-- "ขยายอัตโนมัติ 1 ครั้ง" — the timeout policy of the owner-confirmed limit
-- expansion (migrations 036 / 037) becomes a SETTING instead of a code constant.
--
-- What the switch means (Settings → "ขยายลิมิตอัตโนมัติเมื่อหมดเวลายืนยัน"):
--   true  (default) — an unanswered request is applied as approved each time a
--                     confirmation window lapses (behaviour since 037)
--   false           — the timeout path is allowed to widen the limits by itself
--                     ONCE (one silent rescue per 24 h, ONCE_QUOTA_HOURS); after
--                     that it stops sending new prompts and does NOT widen
--                     anything, and the kill switch closes the book if the
--                     account is still over the widened limits. A refused or
--                     unanswered prompt must never become a permanent exemption
--                     from the risk limits — the way back is Settings + /resume.
--
-- Either way the widening is reported to the owner (LINE + log) — the expansion
-- is never silent. Only a settings write that does NOT land keeps holding the
-- emergency exit (owner decision: "ถ้าเขียน DB ไม่สำเร็จห้ามปิดไม้").
--
-- Idempotent: safe to re-run, and optional — every read path falls back to the
-- AppSettings default (true) so an un-applied migration keeps today's behaviour.

alter table trading_settings
    add column if not exists kill_expand_auto_apply boolean not null default true;

comment on column trading_settings.kill_expand_auto_apply is
    'นโยบายหมดเวลายืนยัน: true = ขยายอัตโนมัติทุกครั้งที่หมดเวลา, false = ขยายให้เองได้ 1 ครั้งใน 24 ชม. แล้วหยุดส่งคำขอใหม่ (ถ้ายังไม่ตอบและยังเกินลิมิต = kill switch ปิดไม้)';
