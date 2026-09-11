-- 032: NO-POSITION-LEFT-BEHIND เป็นไม้ตายหลัก + time stop ยกเว้นไม้กำไร
--
-- ปัญหาเดิม: threshold ของ left_behind = avg_hold × no_behind_hold_mult
-- (ค่าเฉลี่ย ~2.4 วัน × 5.0 = ~11.9 วัน) ซึ่ง "ไกลกว่า" max_hold_days = 5
-- ดังนั้นกฎนี้ไม่มีทางทำงานเลย — time stop (ที่ไม่ดู R) ปิดไม้ทุกใบก่อน
-- รวมทั้งไม้ที่กำไรอยู่ +3R
--
-- แก้:
--   1) no_behind_hold_mult 5.0 → 1.75  (ยิงก่อน time stop 1-2 วัน)
--   2) no_behind_min_days  = 2.0        (floor ของ threshold — กันค่าเฉลี่ยพัง
--                                        แล้ว threshold หลุดต่ำกว่า 1 วัน)
--   3) time_stop_min_r     = 1.0        (ไม้แก่เกิน max_hold_days แต่กำไร
--                                        ≥ 1R จะไม่ถูก time stop ตัด)
--
-- หมายเหตุ: ตัวเลขในข้อ 1-3 ถูกอ่านจาก trading_settings ตอน runtime
-- ถ้าอยากได้ค่าต่างจากนี้ ให้แก้ผ่านหน้า Settings (ไม่ต้องรัน SQL ซ้ำ)
alter table trading_settings
  add column if not exists no_behind_min_days numeric(6, 2) not null default 2.0;

alter table trading_settings
  add column if not exists time_stop_min_r numeric(6, 2) not null default 1.0;

-- อัปเกรดค่า default เดิม (5.0) ให้เป็นค่าใหม่ — เฉพาะแถวที่ยังเป็นค่าเดิม
-- เป๊ะ ๆ จึงไม่ทับค่าที่ผู้ใช้ตั้งเองไว้
update trading_settings
   set no_behind_hold_mult = 1.75
 where id = 1
   and no_behind_hold_mult = 5.0;
