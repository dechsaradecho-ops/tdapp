-- ============================================================
-- Migration 011 — quote API call log (7-day auto-expiry)
-- Run in Supabase SQL Editor (supplements 001-010)
--
-- WHY: every price the system shows comes from an external API
-- (exchangerate-api.com, Yahoo chart, Frankfurter, Twelve Data).
-- This table records EVERY call: which URL, success/error, HTTP
-- status, the price returned, latency, and a MASKED api key hint
-- (first half only + "…" — the second half is never stored).
--
-- RETENTION: rows older than 7 days are deleted automatically by
-- the backend (quote_log.purge_old_logs — throttled, runs on insert
-- and from GET /api/system/quote-logs). No cron needed.
--
-- Safe to re-run (IF NOT EXISTS guards).
-- ============================================================

create table if not exists quote_api_logs (
  id            uuid primary key default uuid_generate_v4(),
  asset         text not null default '',
  category      text not null default 'forex',   -- 'forex' | 'gold'
  provider      text not null default '',        -- exchangerate | yahoo | frankfurter | twelvedata
  url           text not null default '',
  api_key_hint  text not null default '',        -- masked: first half + '…'
  status        text not null default 'success', -- 'success' | 'error'
  http_status   int,
  price         numeric,
  error         text not null default '',
  duration_ms   int,
  created_at    timestamptz not null default now()
);

create index if not exists idx_quote_api_logs_created
  on quote_api_logs(created_at desc);

alter table quote_api_logs enable row level security;
create policy "read quote api logs" on quote_api_logs
  for select using (true);
create policy "write quote api logs service" on quote_api_logs
  for insert to service_role with check (true);
create policy "delete quote api logs service" on quote_api_logs
  for delete to service_role using (true);
