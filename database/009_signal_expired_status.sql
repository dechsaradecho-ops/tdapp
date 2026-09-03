-- 009: allow 'expired' on signals.approval
--
-- The AutoTrader marks pending signals older than 30 minutes as 'expired' so
-- they stop clogging the semi-auto queue and stop re-appearing on the signals
-- page. The enum created in 001_initial_schema.sql only allows
-- ('pending','approved','rejected') — without this migration every UPDATE to
-- 'expired' fails (22P02 invalid input value) and Database.update swallows it,
-- leaving stale signals pending forever.
--
-- Run in Supabase SQL Editor. Safe to re-run (IF NOT EXISTS guard).

alter type approval_status add value if not exists 'expired';
