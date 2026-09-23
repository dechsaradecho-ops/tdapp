-- 053: operational knobs (were hardcoded module constants).
--
-- Every value below mirrors the AppSettings field default in
-- backend/app/models/schemas.py — the Settings page (PUT /api/settings)
-- owns them from here on; code reads them via G(settings, "<field>").
-- Only touches DDL defaults + the live row where it still carries an
-- older value; user-customised rows are never overwritten.
alter table trading_settings add column if not exists signal_ttl_min int not null default 30;
alter table trading_settings add column if not exists auto_trader_batch_limit int not null default 10;
alter table trading_settings add column if not exists kill_expand_step_pct numeric(8,2) not null default 5.0;
alter table trading_settings add column if not exists kill_expand_reask_cooldown_min numeric(10,2) not null default 30.0;
alter table trading_settings add column if not exists kill_expand_reask_after_reject_min numeric(10,2) not null default 120.0;
alter table trading_settings add column if not exists kill_expand_once_quota_hours numeric(10,2) not null default 24.0;
alter table trading_settings add column if not exists kill_expand_fail_notify_min numeric(10,2) not null default 360.0;
alter table trading_settings add column if not exists avg_hold_min_span_days numeric(10,4) not null default 0.05;
alter table trading_settings add column if not exists avg_hold_min_sample int not null default 3;
alter table trading_settings add column if not exists avg_hold_fallback_days numeric(8,2) not null default 4.0;
alter table trading_settings add column if not exists equity_stale_peak_mult numeric(8,2) not null default 3.0;
alter table trading_settings add column if not exists guard_marks_timeout_s numeric(10,2) not null default 20.0;
alter table trading_settings add column if not exists guard_snap_timeout_s numeric(10,2) not null default 30.0;
alter table trading_settings add column if not exists guard_news_timeout_s numeric(10,2) not null default 12.0;
alter table trading_settings add column if not exists guard_atr_proxy_mult numeric(8,4) not null default 0.2;
alter table trading_settings add column if not exists market_analysis_ttl_days int not null default 7;
alter table trading_settings add column if not exists market_analysis_purge_interval_s numeric(12,2) not null default 3600.0;
alter table trading_settings add column if not exists drawdown_approach_ratio numeric(6,4) not null default 0.8;
alter table trading_settings add column if not exists drawdown_approach_cooldown_min numeric(10,2) not null default 360.0;
alter table trading_settings add column if not exists spread_sl_floor_mult numeric(6,2) not null default 3.0;
