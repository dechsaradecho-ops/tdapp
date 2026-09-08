-- 023: max_hold_days — time stop (ปิดไม้ที่ถือเกินกำหนด)
-- Strategy E: positions held longer than max_hold_days are closed by
-- position_guard with close_reason = 'time'. 0 disables the feature.
alter table trading_settings
  add column if not exists max_hold_days int not null default 5;
