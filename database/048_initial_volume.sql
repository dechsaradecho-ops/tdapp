-- 048_initial_volume.sql
-- Remember the ORIGINAL size of a paper position so a partial close can be
-- shown as "closed / original" (e.g. 0.01/0.02) and the remaining size is
-- unambiguous.
--
-- WHY: migration 047 made the guard persist the REDUCED volume after a TP1 /
-- smart-exit scale-out, so `paper_trades.volume` now means "size still open".
-- Without the original size the monitor cannot say how much was closed:
--   * the closed-trade row showed only the remaining 0.01 (looked like the
--     whole position was 0.01),
--   * the open row showed 0.01 with no hint that it started at 0.02.
--
-- `initial_volume` is written once at insert (record_trade) and backfilled by
-- the guard for legacy rows that predate this migration.

alter table paper_trades
    add column if not exists initial_volume numeric;

-- Backfill: rows that never partial-closed keep their current volume as the
-- original; rows that DID partial-close are left NULL (the guard backfills
-- them from the order_opened signal-log when it next manages the position).
update paper_trades
   set initial_volume = volume
 where initial_volume is null
   and coalesce(partial_done, false) = false;

comment on column paper_trades.initial_volume is
    'Original position size at open. volume = remaining after partial closes; '
    'initial_volume lets the UI show closed/original (e.g. 0.01/0.02).';
