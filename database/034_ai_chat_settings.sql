-- 034: ตั้งค่า AI chat (model name + base URL) ได้จากหน้า Settings
--
-- เดิม model/url ของ AI มาจาก backend/ai.config.json เท่านั้น → เปลี่ยน
-- โมเดลต้องแก้ไฟล์ + redeploy ใหม่ทั้ง service
--
-- เพิ่ม 2 คอลัมน์ใน trading_settings (แถวเดียว id=1 ที่หน้า Settings ใช้อยู่แล้ว)
--   ai_model     = ''  → ว่าง = ใช้ค่าจาก ai.config.json / ค่า default ของ provider
--   ai_base_url  = ''  → ว่าง = ใช้ค่าจาก ai.config.json / ค่า default ของ provider
--
-- ลำดับความสำคัญ:  Settings page (DB)  >  ai.config.json  >  default ของ provider
-- ค่ามีผลทันทีที่กดบันทึก (ไม่ต้อง redeploy) เพราะ backend เคลียร์ cache ของ
-- AIConfig แล้วสร้าง provider ใหม่ในคำขอถัดไป
--
-- ทั้งสองคอลัมน์เป็น NON-SECRET — คีย์ API ยังอยู่ใน env var AI_API_KEY ของ
-- service เท่านั้น (ห้ามเก็บคีย์ใน DB/ไฟล์)
--
-- ai_base_url ต้องเป็น base ของ OpenAI-compatible API เช่น
--   https://api.deepseek.com
--   https://opencode.ai/zen/go/v1
-- (client ต่อท้ายด้วย /chat/completions เอง — ถ้าวาง URL เต็มมา ระบบตัด
--  ส่วน /chat/completions ที่เกินออกให้อัตโนมัติ)
alter table trading_settings
  add column if not exists ai_model text not null default '';

alter table trading_settings
  add column if not exists ai_base_url text not null default '';

comment on column trading_settings.ai_model is
  'ชื่อโมเดล AI ที่ใช้ตอบแชท (ว่าง = ใช้ค่าใน ai.config.json)';
comment on column trading_settings.ai_base_url is
  'base URL ของ AI gateway แบบ OpenAI-compatible (ว่าง = ใช้ค่าใน ai.config.json)';
