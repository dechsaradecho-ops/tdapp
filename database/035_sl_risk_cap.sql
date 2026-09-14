-- ============================================================
-- Migration 035 — SL risk cap switch (SL กว้างแค่ไหนก็ไม่เกินงบ)
-- Run in Supabase SQL Editor (supplements 001-034)
-- ============================================================

-- min_lot floor ดันความเสี่ยงจริงเกินงบเมื่อ SL กว้าง (prod AUDNZD:
-- SL 0.00676 x floor 0.02 x 100k = $13.52 = 6.76% เทียบงบ 4% = $8)
-- เมื่อเปิด (default true) execute_signal รัด SL ลงมาที่
--   งบ (capital x risk_per_trade_pct) / (floor x contract)
-- ก่อน sizing (ไม่ขยาย SL ที่แคบอยู่แล้ว) และ TP คำนวณใหม่ที่ RR เดิม
-- การ์ดสัญญาณ preview ใช้ helper เดียวกัน ค่าจึงตรงกับ order จริง
alter table trading_settings
  add column if not exists sl_cap_enabled boolean not null default true;

comment on column trading_settings.sl_cap_enabled is
  'รัด SL กว้างลงมาให้เสี่ยงไม่เกินงบ (true = เปิด, false = ใช้ SL เดิม)';
