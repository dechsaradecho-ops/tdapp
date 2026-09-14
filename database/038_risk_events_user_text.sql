-- ============================================================
-- Migration 038 — risk_events.user_id: uuid FK → text (audit ลงจริง)
-- Run in Supabase SQL Editor (supplements 001–037)
-- ============================================================
--
-- WHY (prod 2026-09-14): portfolio_monitor เขียน audit ตอนลิมิตเกิน
-- (limit_breach) และ limit_expand เขียนตอนเจ้าของอนุมัติ/ปฏิเสธ
-- (limit_expanded / limit_expand_rejected) ลงตาราง risk_events ทุกครั้ง —
-- แต่ตารางนี้ถูกสร้างใน 001 ด้วย `user_id uuid not null references users(id)`
-- ขณะที่แอปรันด้วย pseudo-user 'demo' (text) ผลคือ PostgREST ตอบ
--     invalid input syntax for type uuid: "demo"   (code 22P02)
-- และ Database.insert กลืน error ไว้ที่ log.debug → risk_events ว่างเปล่า
-- ตลอดทั้งสองทาง (ตาราง users เองก็ไม่มี row เลย เพราะระบบใช้ PIN auth
-- ไม่ได้ใช้ Supabase Auth)
--
-- วิธีแก้ = แบบเดียวกับ 007/017/036 ที่เลี่ยงกับดักนี้มาก่อน: เก็บ user_id
-- เป็น text (deployment นี้มีเจ้าของบัญชีคนเดียว) ไม่ลบข้อมูลเดิม — และ
-- risk_events ตั้งใจให้เป็น "ประวัติถาวร" ไม่มี TTL จึงไม่ต้องแปลง/ทิ้งอะไร
alter table risk_events drop constraint if exists risk_events_user_id_fkey;
alter table risk_events alter column user_id type text using user_id::text;
alter table risk_events alter column user_id set default 'demo';

comment on column risk_events.user_id is
  'เจ้าของเหตุการณ์ (text) — backend ใช้ pseudo-user demo ไม่ใช่ uuid ของ Supabase Auth';

-- RLS: 001 สร้าง policy "own risk events" ด้วย `auth.uid() = user_id`
-- ตอนนี้ user_id เป็น text แล้ว → พอมี request ที่ไม่ใช่ service_role
-- (anon/publishable key) RLS จะถูกประเมินและ error ทันที
--     operator does not exist: uuid = text
-- จึงทิ้ง policy เดิมแล้วใช้รูปเดียวกับ kill_expand_requests ใน 036:
-- อ่านเปิด (หน้าเว็บ/static) เขียนเฉพาะ service role
drop policy if exists "own risk events" on risk_events;
drop policy if exists "read risk events all" on risk_events;
create policy "read risk events all"
    on risk_events for select
    using (true);

-- 004 ให้สิทธิ์เขียนกับ service_role เฉพาะ worker table
-- (market_analysis / signals / news_analysis / ai_daily_report / db_probe)
-- แต่ลืม risk_events ซึ่งเป็นตารางที่ worker เขียนเหมือนกัน → เพิ่มให้ครบ
-- ตามแบบฉบับของ 004 (policy ของ service_role ไม่มีผลเมื่อคีย์นั้น bypass RLS
-- แต่ถ้า deployment ไหนยิงด้วย anon key การเขียนจะถูกปฏิเสธแบบเงียบ ๆ)
drop policy if exists "write risk events service" on risk_events;
create policy "write risk events service" on risk_events
    for insert to service_role with check (true);

drop policy if exists "update risk events service" on risk_events;
create policy "update risk events service" on risk_events
    for update to service_role using (true) with check (true);

drop policy if exists "delete risk events service" on risk_events;
create policy "delete risk events service" on risk_events
    for delete to service_role using (true);
