-- 051: align no_behind_hold_mult column DEFAULT with AppSettings (1.75).
--
-- History: 029 added the column with DEFAULT 5.0; 032 updated the live
-- single row (id=1) from 5.0 → 1.75 but left the column DEFAULT at 5.0,
-- so a recreated row would silently get the dead threshold again
-- (avg_hold × 5.0 ≈ 11.9 days > max_hold 5 → left-behind never fires).
-- AppSettings default is 1.75 (schemas.py) — fix the DDL default to match.
-- Only touches the default; existing rows (incl. user-customised) unchanged.
alter table trading_settings
  alter column no_behind_hold_mult set default 1.75;
