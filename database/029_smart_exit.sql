-- 029 Smart Exit Engine settings (matches backend AppSettings defaults).
alter table trading_settings add column if not exists smart_exit_enabled boolean not null default true;
alter table trading_settings add column if not exists exit_score_close numeric(6,2) not null default 45.0;
alter table trading_settings add column if not exists profit_protect_r numeric(6,2) not null default 2.0;
alter table trading_settings add column if not exists reversal_opp_min numeric(6,2) not null default 50.0;
alter table trading_settings add column if not exists news_exit_enabled boolean not null default true;
alter table trading_settings add column if not exists news_exit_min_r numeric(6,2) not null default 1.0;
alter table trading_settings add column if not exists volatility_exit_atr numeric(6,2) not null default 2.5;
alter table trading_settings add column if not exists no_behind_min_r numeric(6,2) not null default 0.5;
alter table trading_settings add column if not exists no_behind_hold_mult numeric(6,2) not null default 5.0;
alter table trading_settings add column if not exists trailing_ladder boolean not null default true;
