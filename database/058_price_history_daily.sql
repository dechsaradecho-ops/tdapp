-- 058_price_history_daily.sql
-- Daily OHLC history in Postgres — fetch once, reuse across simulation runs.
--
-- WHY (owner 2026-10-06)
-- ----------------------
-- Every simulation run re-fetched ~2 years of daily candles from Yahoo and
-- threw them away. Worse, nothing proved two runs saw the same prices. This
-- table stores one row per (asset, bar_date); each sync fetches only what is
-- missing (the gap since the latest stored bar, plus a refresh of the latest
-- bar itself while it may still be forming intraday). Bars older than the
-- latest stored one are IMMUTABLE — never updated — so a re-run on the same
-- dates sees the same prices. A feed that disagrees with a frozen bar is
-- reported as a conflict and NOT written (see app/services/price_history.py).
--
-- Bars are keyed by UTC date, not timestamp: all three feeds (Yahoo epoch
-- seconds, Frankfurter ISO days, TwelveData datetimes) reduce to a day, and
-- a day is what the replay indexes on (`bar_index` counts trading days).

create table if not exists price_history_daily (
    asset      text not null,
    bar_date   date not null,
    open       numeric not null,
    high       numeric not null,
    low        numeric not null,
    close      numeric not null,
    source     text not null default 'yahoo',
    created_at timestamptz not null default now(),
    primary key (asset, bar_date)
);

create index if not exists price_history_daily_asset_date_idx
    on price_history_daily (asset, bar_date desc);

-- ---------------------------------------------------------------------------
-- RLS — service_role ONLY (same posture as migration 057).
--
-- The frontend never creates a Supabase client — every read/write goes
-- through the backend API with the service key — so no browser-side path
-- needs a policy. With RLS enabled and no policy for them, anon and
-- authenticated get nothing; service_role bypasses RLS by design.
-- ---------------------------------------------------------------------------
alter table price_history_daily enable row level security;

drop policy if exists "price_history_service_all" on price_history_daily;
create policy "price_history_service_all"
    on price_history_daily for all
    to service_role
    using (true)
    with check (true);

revoke all on price_history_daily from anon, authenticated;
