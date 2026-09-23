-- 050: opportunity_score บน market_analysis — แยกแกน Opportunity ออกจาก confidence
-- ปัญหา: market_analysis มีแค่คอลัมน์ confidence คอลัมน์เดียว scanner จึงเขียน
-- opp.confidence ลง confidence แล้ว market.py/portfolio.py อ่าน score กลับจาก
-- confidence คอลัมน์เดียวกัน → Opportunity Score กับ Confidence Score เป็นเลข
-- เดียวกันเสมอ (P1-3 คำนวณแยกกันแล้วใน engine แต่ DB รวมกลับเป็นค่าเดียว)
-- หลัง migration นี้: confidence = evidence agreement (เดิม), opportunity_score
-- = setup quality (ใหม่, nullable เพื่อให้แถวเก่าก่อน 050 ยังอ่านได้ — reader
-- fallback เป็น confidence เหมือนเดิม). scanner เขียน resilient (drop column
-- แล้ว retry ถ้า PostgREST ยังไม่มีคอลัมน์ → แถวไม่หายทั้งแถว)
-- RUN IN SUPABASE SQL EDITOR.
alter table market_analysis
  add column if not exists opportunity_score numeric(5, 2);
