-- 010: stamp when a signal was approved (shown on the signals page).
-- Existing rows get their approval time backfilled from the trade journal
-- where possible; older ones without history keep NULL (UI shows the
-- created time instead).
alter table signals add column if not exists approved_at timestamptz;
