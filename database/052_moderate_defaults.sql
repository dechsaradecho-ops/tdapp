-- 052: align trading_settings defaults + live row with moderate preset
-- for frequency and RR (owner decision 2026-09-23).
--
-- AppSettings defaults changed: max_trades_daily 6 → 10,
-- max_open_positions 4 → 12, risk_per_trade_pct 1.0 → 2.0, rr_target 2.0 → 1.5.
-- This migration moves the DDL defaults and the live single row (id=1)
-- only where it still carries the OLD defaults, so user-customised rows
-- are never overwritten.
alter table trading_settings alter column max_trades_daily set default 10;
alter table trading_settings alter column max_open_positions set default 12;
alter table trading_settings alter column risk_per_trade_pct set default 2.0;
alter table trading_settings alter column rr_target set default 1.5;

update trading_settings set max_trades_daily = 10
 where id = 1 and max_trades_daily = 6;
update trading_settings set max_open_positions = 12
 where id = 1 and max_open_positions = 4;
update trading_settings set risk_per_trade_pct = 2.0
 where id = 1 and risk_per_trade_pct = 1.0;
update trading_settings set rr_target = 1.5
 where id = 1 and rr_target = 2.0;
