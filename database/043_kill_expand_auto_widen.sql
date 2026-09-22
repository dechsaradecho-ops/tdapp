-- 043_kill_expand_auto_widen.sql
-- P0-3 (fail-closed timeout): a LAPSED (unanswered) kill-expand confirmation
-- window must NEVER widen a risk limit by itself. Before this change the
-- timeout path (app/services/limit_expand.py::settle_expired) auto-applied the
-- +5pp expansion when kill_expand_auto_apply was ON (default since 039), so an
-- unattended weekend could silently raise a daily-loss / drawdown limit and
-- keep trading. New rule: silence keeps the ORIGINAL limits and stays PAUSED
-- (new orders blocked — fail-closed); the owner is warned once and the kill
-- switch / position guard keep protecting any open book. Widening on a timeout
-- is now an explicit opt-in (kill_expand_auto_widen = true), which re-enables
-- the 039 flow (kill_expand_auto_apply: every-time vs one-shot).
--
-- The column DEFAULT is false so a fresh row and the live row both land in the
-- fail-closed state after this migration; existing rows are backfilled with the
-- default. This only affects the TIMEOUT path — a manual /dd_ok (or popup
-- Approve) still widens exactly as before.

alter table trading_settings
  add column if not exists kill_expand_auto_widen boolean not null default false;

comment on column trading_settings.kill_expand_auto_widen is
  'P0-3 fail-closed timeout: false (default) => a lapsed confirmation window '
  'keeps the ORIGINAL risk limits and stays paused; true => the 039 flow '
  '(kill_expand_auto_apply) may widen on a timeout.';

-- Backfill any pre-existing NULLs (defensive; the column is not-null default).
update trading_settings
  set kill_expand_auto_widen = false
  where kill_expand_auto_widen is null;
