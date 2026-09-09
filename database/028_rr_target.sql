-- 028: rr_target — Reward:Risk target ที่ผู้ใช้ปรับได้ (Settings page)
-- TP = SL distance × rr_target สำหรับทุกสัญญาณใหม่ (default 2.0 = 1:2)
alter table trading_settings
  add column if not exists rr_target numeric(6, 2) not null default 2.0;
