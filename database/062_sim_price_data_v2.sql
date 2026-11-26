-- 062_sim_price_data_v2.sql
-- Versioned simulation price data (V2 datasets).
--
-- WHY
-- ---
-- The V1 `price_history_daily` table (migration 058) keys bars by
-- (asset, bar_date) with no dataset version, so two simulation runs on
-- different fetch dates can silently see different prices. V2 stores every
-- bar under a `dataset_version`: a dataset is an immutable snapshot with
-- its own coverage report, data hash, and code version, so a run can pin
-- the exact prices it replayed.
--
-- Source split (binding): Biquote is authoritative for the most recent 500
-- daily bars per asset; Twelve Data fills older history (default five
-- years total). Every bar stores its `source`.
--
-- Idempotent: safe to re-run (`if not exists` throughout); never touches
-- existing rows.

-- ---------------------------------------------------------------------------
-- Bars: one row per (dataset_version, asset, bar_date).
-- ---------------------------------------------------------------------------
create table if not exists sim_price_bars_v2 (
    dataset_version  text not null,
    asset            text not null,
    bar_date         date not null,
    open             numeric not null,
    high             numeric not null,
    low              numeric not null,
    close            numeric not null,
    source           text not null check (source in ('biquote', 'twelvedata')),
    source_timestamp timestamptz,
    ingested_at      timestamptz not null default now(),
    primary key (dataset_version, asset, bar_date)
);

create index if not exists sim_price_bars_v2_version_asset_date_idx
    on sim_price_bars_v2 (dataset_version, asset, bar_date desc);

comment on table sim_price_bars_v2 is
    'V2 versioned daily OHLC bars; immutable per dataset_version.';
comment on column sim_price_bars_v2.source is
    'Authoritative feed for this bar: biquote (recent 500 bars) or twelvedata (older history).';

-- ---------------------------------------------------------------------------
-- Dataset metadata: one row per dataset_version.
-- ---------------------------------------------------------------------------
create table if not exists sim_dataset_meta_v2 (
    dataset_version text not null primary key,
    status          text not null default 'building'
        check (status in ('building', 'complete', 'failed')),
    years           numeric,
    assets          jsonb,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    report          jsonb,
    data_hash       text,
    code_version    text
);

create index if not exists sim_dataset_meta_v2_version_status_idx
    on sim_dataset_meta_v2 (dataset_version, status);

comment on table sim_dataset_meta_v2 is
    'V2 dataset snapshots: version, date range, coverage, data hash, code version.';

-- ---------------------------------------------------------------------------
-- RLS — service_role ONLY (same posture as migrations 057/058).
--
-- The frontend never creates a Supabase client — every read/write goes
-- through the backend API with the service key — so no browser-side path
-- needs a policy. With RLS enabled and no policy for them, anon and
-- authenticated get nothing; service_role bypasses RLS by design.
-- ---------------------------------------------------------------------------
alter table sim_price_bars_v2 enable row level security;
alter table sim_dataset_meta_v2 enable row level security;

drop policy if exists "sim_price_bars_v2_service_all" on sim_price_bars_v2;
create policy "sim_price_bars_v2_service_all"
    on sim_price_bars_v2 for all
    to service_role
    using (true)
    with check (true);

drop policy if exists "sim_dataset_meta_v2_service_all" on sim_dataset_meta_v2;
create policy "sim_dataset_meta_v2_service_all"
    on sim_dataset_meta_v2 for all
    to service_role
    using (true)
    with check (true);

revoke all on sim_price_bars_v2 from anon, authenticated;
revoke all on sim_dataset_meta_v2 from anon, authenticated;
