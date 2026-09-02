-- ============================================================
-- Migration 002 — worker pipeline tables
-- Run in Supabase SQL Editor (supplements 001_initial_schema.sql)
-- ============================================================

-- Worker #2 output: AI news sentiment per event (read by market scanner)
create table if not exists news_analysis (
  id              uuid primary key default uuid_generate_v4(),
  event           text not null,
  sentiment       numeric(4, 2) not null default 0,
  affected_assets jsonb not null default '[]'::jsonb,
  analysis        text not null default '',
  confidence      numeric(5, 2) not null default 0,
  created_at      timestamptz not null default now()
);
create index if not exists idx_news_analysis_created
  on news_analysis(created_at desc);

-- Daily AI market report log
create table if not exists ai_daily_report (
  id          uuid primary key default uuid_generate_v4(),
  report_date date not null default current_date,
  summary     text not null default '',
  created_at  timestamptz not null default now()
);

-- Shared market data: API reads, worker writes
alter table news_analysis   enable row level security;
alter table ai_daily_report enable row level security;
create policy "read news analysis"   on news_analysis   for select using (true);
create policy "read ai daily report" on ai_daily_report for select using (true);
