-- 024: gold_breakout_only — Strategy D (breakout-retest gate เฉพาะทอง)
-- true  = XAUUSD เปิดสัญญาณเฉพาะเมื่อ breakout 20-bar high หรือ retest สำเร็จ
--         (SL ยึดแนวที่ทะลุ + buffer 0.5×ATR)
-- false = พฤติกรรมเดิม (ทองเทรดเหมือนคู่อื่น)
alter table trading_settings
  add column if not exists gold_breakout_only boolean not null default true;
