-- 061: gold volatility gate threshold (XAUUSD ATR% ceiling).
-- Mirrors AppSettings.gold_max_atr_pct in backend/app/models/schemas.py;
-- later tasks consume it, 0 forbids every new XAUUSD order path.
-- Idempotent: safe to re-run; never touches existing rows' custom values.
alter table trading_settings add column if not exists gold_max_atr_pct numeric(6,2) not null default 3.5;
do $$ begin
  if not exists (
    select 1 from pg_constraint where conname = 'trading_settings_gold_max_atr_pct_range'
  ) then
    alter table trading_settings
      add constraint trading_settings_gold_max_atr_pct_range
      check (gold_max_atr_pct >= 0 and gold_max_atr_pct <= 10);
  end if;
end $$;
