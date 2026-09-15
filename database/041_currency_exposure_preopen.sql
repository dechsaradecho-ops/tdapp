-- 041_currency_exposure_preopen.sql
--
-- Pro-trader survival guards #1 + #3 (integrated into the existing gate
-- pipeline, not a parallel system):
--
--   #1 Currency exposure cap (Gate 4b)
--      Correlation cap (Gate 4) averages PAIRWISE correlation, so AUDNZD +
--      AUDCHF (0.55 prior) can each look "diversified" while the account is
--      really ONE AUD bet. This gate sums the RISK (USD at the stop) per
--      currency and per direction over the open book + the candidate order,
--      and blocks when one bucket exceeds this % of capital.
--      0 = ปิดฟีเจอร์
--
--   #3 Pre-open guards (Gate 3b) — three independent checks:
--      a) spread_guard_max_pct — block when the symbol's spread eats more
--         than this % of the SL distance (0.00035 spread on a 0.0007 SL =
--         50%: the edge is gone before the trade starts). 0 = ปิด.
--      b) pre_news_flatten_min — block a NEW order when a high-impact event
--         for one of the pair's currencies lands within this many minutes.
--         0 = ใช้เฉพาะ block window ของ news gate เดิม.
--      c) session_filter_enabled — block new orders while the market is
--         closed (weekend gap) or only a low-liquidity session is open
--         (Sydney-only hours). false = ปิด.
--
-- Defaults are ON (user decision 2026-09-15: "เปิดเป็น default") and match the
-- moderate risk preset, so existing rows pick up the guards on the next
-- settings read. Set any value to 0/false in Settings to disable that guard.
--
-- Idempotent: safe to re-run. Every read path falls back to the AppSettings
-- default, so an un-applied migration keeps the new defaults. Settings save
-- tolerates PGRST204 (drops unknown column with a warning).

alter table trading_settings
    add column if not exists max_currency_exposure_pct numeric not null default 50.0;

alter table trading_settings
    add column if not exists spread_guard_max_pct numeric not null default 25.0;

alter table trading_settings
    add column if not exists pre_news_flatten_min integer not null default 30;

alter table trading_settings
    add column if not exists session_filter_enabled boolean not null default true;

comment on column trading_settings.max_currency_exposure_pct is
    'เพดานความเสี่ยงต่อสกุล (%) — Gate 4b: รวม risk-at-stop ต่อสกุล+ทิศทาง (ไม้เปิด + ไม้นี้) ห้ามเกิน % ของทุน — 0 = ปิด (default 50)';

comment on column trading_settings.spread_guard_max_pct is
    'เพดานสเปรดต่อระยะ SL (%) — Gate 3b: สเปรดกินระยะ SL เกิน % นี้ = edge หาย งดเปิดไม้ — 0 = ปิด (default 25)';

comment on column trading_settings.pre_news_flatten_min is
    'งดเปิดไม้ใหม่ก่อนข่าว high-impact กี่นาที (นาที) — Gate 3b: เฉพาะข่าวของสกุลในคู่นั้น — 0 = ปิด (default 30)';

comment on column trading_settings.session_filter_enabled is
    'ตัวกรอง session — Gate 3b: งดเปิดไม้ใหม่ตอนตลาดปิด (weekend) หรือสภาพคล่องต่ำ (Sydney-only) — false = ปิด (default true)';
