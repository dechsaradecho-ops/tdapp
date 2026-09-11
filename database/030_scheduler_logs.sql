-- ============================================================
-- Migration 030 — scheduler run log (7-day auto-expiry)
-- Run in Supabase SQL Editor (supplements 001-029)
--
-- WHY: workers run silently in-process (Render tdapp-api,
-- ENABLE_WORKERS=1). When SL doesn't move or no signals appear,
-- the Logs page had no way to show whether the scheduler itself
-- is alive. This table logs ONE row per job tick:
--
--   market_scanner | news_analysis | portfolio_monitor |
--   notifications  | auto_trader   | position_guard    |
--   calendar_sync  | daily_digest
--
-- RETENTION: rows older than 7 days are deleted automatically by
-- the backend (scheduler_log.purge_old_logs — throttled, runs from
-- GET /api/system/scheduler-logs). No cron needed.
--
-- Safe to re-run (IF NOT EXISTS guards).
-- ============================================================

create table if not exists scheduler_runs (
  id            uuid primary key default uuid_generate_v4(),
  job_id        text not null default '',
  status        text not null default 'ok',      -- 'ok' | 'error'
  duration_ms   int,
  detail        text not null default '',
  error         text not null default '',
  created_at    timestamptz not null default now()
);

create index if not exists idx_scheduler_runs_created
  on scheduler_runs(created_at desc);
create index if not exists idx_scheduler_runs_job
  on scheduler_runs(job_id, created_at desc);

alter table scheduler_runs enable row level security;
create policy "read scheduler runs" on scheduler_runs
  for select using (true);
create policy "write scheduler runs service" on scheduler_runs
  for insert to service_role with check (true);
create policy "delete scheduler runs service" on scheduler_runs
  for delete to service_role using (true);
