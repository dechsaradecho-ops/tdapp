-- ============================================================
-- Migration 004 — RLS INSERT/UPDATE/DELETE policies for worker tables
-- Run in Supabase SQL Editor (supplements 001/002/003)
--
-- WHY: 001/002 enable RLS on the worker tables but only create
-- `for select using (true)` policies. Postgres RLS defaults to DENY for
-- anything without a matching policy, so every INSERT from the backend
-- workers (market_scanner, news_analysis) was silently rejected with:
--   "new row violates row-level security policy for table ..."
--
-- The backend authenticates with the secret key (sb_secret_... /
-- service_role), so these policies grant writes to `service_role`.
-- ============================================================

-- ---------- market_analysis (written by market scanner) ----------
drop policy if exists "write market analysis service" on market_analysis;
create policy "write market analysis service" on market_analysis
  for insert to service_role with check (true);

drop policy if exists "update market analysis service" on market_analysis;
create policy "update market analysis service" on market_analysis
  for update to service_role using (true) with check (true);

drop policy if exists "delete market analysis service" on market_analysis;
create policy "delete market analysis service" on market_analysis
  for delete to service_role using (true);

-- ---------- signals (written by scanner, approved by users) ----------
drop policy if exists "write signals service" on signals;
create policy "write signals service" on signals
  for insert to service_role with check (true);

drop policy if exists "update signals service" on signals;
create policy "update signals service" on signals
  for update to service_role using (true) with check (true);

-- ---------- news_analysis (written by news worker) ----------
drop policy if exists "write news analysis service" on news_analysis;
create policy "write news analysis service" on news_analysis
  for insert to service_role with check (true);

-- ---------- ai_daily_report (written by daily report worker) ----------
drop policy if exists "write ai daily report service" on ai_daily_report;
create policy "write ai daily report service" on ai_daily_report
  for insert to service_role with check (true);

-- ---------- db_probe (dev probe table from 003) ----------
drop policy if exists "write db probe service" on db_probe;
create policy "write db probe service" on db_probe
  for insert to service_role with check (true);
drop policy if exists "delete db probe service" on db_probe;
create policy "delete db probe service" on db_probe
  for delete to service_role using (true);
