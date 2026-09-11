-- 033: ต้นทุนขาออกของการเทรดกระดาษ (exit spread + commission)
--
-- ปัญหาที่พบจากการตรวจ prod (2026-09-11):
--   ระบบจำลอง "ขาเข้า" อย่างถูกต้องอยู่แล้ว (apply_spread เติมครึ่ง spread
--   ฝั่ง entry ตาม DEFAULT_SPREADS ต่อคู่เงิน) แต่ "ขาออก" ไม่มีต้นทุนเลย —
--   ปิดไม้ที่ราคา mark ตรง ๆ และไม่มีค่าคอมมิชชั่น ทำให้ PnL กระดาษสวยเกินจริง
--   (หลักฐาน: 7 ไม่ที่ปิดบน prod มีไม่ถึง 2 ไม้ที่ขาดทุนเกิน 1/4 spread
--    และมีไม้ที่บันทึกกำไร +10.5 ทั้งที่โดน trailing stop)
--
-- เพิ่ม 2 คอลัมน์:
--   paper_exit_spread_mult     = 0.5  → คิดครึ่ง spread ตอนออก (รวมกับครึ่ง
--                                      ฝั่งเข้า = 1 spread เต็มต่อ round trip)
--   paper_commission_per_lot   = 3.5  → ค่าคอมต่อ side ต่อ 1.00 standard lot
--                                      (round turn = 2 เท่า)
--
-- ต้นทุนถูก "หักจาก PnL ที่บันทึก" ไม่ได้แก้ราคาปิดที่บันทึกไว้ → กราฟ/หน้าตรวจสอบ
-- ยังเทียบกับราคาตลาดได้ตรงเหมือนเดิม
--
-- ปรับ/ปิดได้ผ่านหน้า Settings (ช่องตัวเลขด้านบน) โดยไม่ต้องรัน SQL ซ้ำ
-- ตั้ง paper_exit_spread_mult = 0 และ paper_commission_per_lot = 0
-- เพื่อกลับไปพฤติกรรมเดิม (ก่อน 2026-09-11)
alter table trading_settings
  add column if not exists paper_exit_spread_mult numeric(6, 3) not null default 0.5;

alter table trading_settings
  add column if not exists paper_commission_per_lot numeric(8, 2) not null default 3.5;

comment on column trading_settings.paper_exit_spread_mult is
  'ตัวคูณ spread ขาออก (0.5 = ปิดครึ่ง round trip, >0.5 = บวก slippage ตอน stop out)';
comment on column trading_settings.paper_commission_per_lot is
  'ค่าคอมต่อ side หน่วย USD ต่อ 1.00 standard lot (FX 100k / ทอง 100 oz)';
