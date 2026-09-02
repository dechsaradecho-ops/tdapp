-- ============================================================
-- Migration 003: db_probe — scratch table for the live DB read/write
-- self-test (GET /api/system/db-check). No RLS, no FKs: the ONLY goal
-- is to prove the service key can insert/select/delete. The endpoint
-- cleans up after itself; stray rows (if a run dies mid-probe) can be
-- removed manually: delete from db_probe where note = 'db-check';
-- ============================================================

create table if not exists db_probe (
  id         uuid primary key default uuid_generate_v4(),
  token      text not null,
  note       text not null default '',
  created_at timestamptz not null default now()
);

-- Deliberately NO row level security / policies: this table is for the
-- backend service key only. Nothing sensitive is ever stored here.
