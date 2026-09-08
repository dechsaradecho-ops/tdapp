-- 026: score_reasons บน market_analysis — breakdown การคำนวณ Opportunity Score
-- ทั้งหมด (ทุก component line จาก StrategyEngine.opportunity_score) คั่นด้วย \n
-- หน้า home การ์ด Opportunity Score กดเปิด popup ดู "ที่มาของคะแนน" ได้
-- (explanation เดิมเก็บแค่ 3 เหตุผลแรก join ด้วย " | ")
-- RUN IN SUPABASE SQL EDITOR.
alter table market_analysis
  add column if not exists score_reasons text not null default '';
