-- ============================================================
-- Migration 037 — ระยะเวลารอการยืนยันขยายลิมิตความเสี่ยง (นาที)
-- Run in Supabase SQL Editor (supplements 001-036)
-- ============================================================

-- เวลาที่ยังรอคำตอบได้ก่อนที่ "คำขอยืนยันขยายลิมิต" จะหมดอายุ
-- (kill_expand_requests ของ migration 036) — ตั้งได้ในหน้า Settings
-- ระบบส่งคำขอพร้อมค่า metric ณ วินาทีนั้น (drawdown / loss) ถ้าผ่านไปนาน
-- เกินค่านี้ ตัวเลขที่อ้างในข้อความ LINE (หรือ popup) ถือว่าเก่าเกินไป
-- แถวนั้นจะถูกปิดเป็น 'expired' และ monitor จะสร้างคำขอใหม่ (พร้อมค่าใหม่)
-- แทน — ข้อความเก่าใน LINE จึงไม่สามารถขยายลิมิตได้ตลอดไป
--
-- ค่านี้ต้องเท่ากับ AppSettings.kill_expand_ttl_min (backend) และ
-- DEFAULT_SETTINGS.limit_expand_ttl_min (frontend) — default 180 นาที
-- ค่าที่น้อยกว่า/เท่ากับ 0 จะถูก clamp ที่ฝั่ง backend ให้เป็น 5 นาที
-- (สั้นสุด) ไม่ใช่ "ไม่มีวันหมดอายุ"
alter table trading_settings
  add column if not exists kill_expand_ttl_min integer not null default 180;

comment on column trading_settings.kill_expand_ttl_min is
  'นาทีที่รอการยืนยันขยายลิมิตความเสี่ยงก่อนคำขอหมดอายุ (default 180)';
