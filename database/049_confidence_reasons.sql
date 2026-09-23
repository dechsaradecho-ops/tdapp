-- 049: confidence_reasons บน market_analysis — breakdown ของ Confidence Score
-- (P1-3 evidence agreement) แยกจาก score_reasons ที่เก็บ Opportunity Score
-- คั่นด้วย \n เหมือน score_reasons; หน้า home การ์ด
-- "Opportunity Score & Confidence Score" กดเปิด popup ดูที่มาของทั้งสองแกนได้
-- (ก่อนหน้านี้ confidence_reasons คำนวณแล้วแต่ไม่ถูกเก็บลง DB → popup
--  อธิบายได้แค่ฝั่ง Opportunity Score)
-- RUN IN SUPABASE SQL EDITOR.
alter table market_analysis
  add column if not exists confidence_reasons text not null default '';
