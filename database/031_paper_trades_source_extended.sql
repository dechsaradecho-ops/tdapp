-- 031: paper_trades.source must accept 'extended'
--
-- WHY: migration 007 created the column as
--     source text not null default 'auto' check (source in ('auto','approved'))
-- but the Extended page's "เปิดออเดอร์ (ขา Market แรก)" button opens orders
-- through POST /api/trading/extended-open with source='extended'.
--
-- Result on prod (2026-09-11): every insert of an Extended order was rejected
-- with
--     23514  new row for relation "paper_trades" violates check constraint
--            "paper_trades_source_check"
-- and `Database.insert` swallows errors into a log line, so the order existed
-- on the broker + LINE + signal_logs but had NO journal row — which is why the
-- position (e.g. PAPER-000041 AUDCHF) never appeared on the /monitor page and
-- never counted in the monitor stats / PnL / win-rate.
--
-- Safe + idempotent: drops the old check and re-adds it with 'extended' added.
-- Existing rows ('auto', 'approved') stay valid.
--
-- Run on Supabase (SQL editor) once. `record_trade` also carries a
-- deploy-safety fallback that journals as 'approved' while this is missing.

alter table paper_trades drop constraint if exists paper_trades_source_check;

alter table paper_trades add constraint paper_trades_source_check
  check (source in ('auto', 'approved', 'extended'));
