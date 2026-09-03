-- 008_pin_auth.sql — 6-digit PIN gate for the dashboard
-- Run once in the Supabase SQL editor (idempotent — safe to re-run).
--
-- Single row (id=1). The backend reads/writes it via service_role.
-- RLS is enabled with NO policies: anon/authenticated keys are denied
-- entirely (nobody should read pin_hash over the public API).

create table if not exists app_auth (
  id              int primary key default 1 check (id = 1),
  pin_hash        text        not null default '',
  salt            text        not null default '',
  failed_attempts int         not null default 0,
  locked_until    timestamptz,
  pin_set_at      timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

insert into app_auth (id) values (1) on conflict (id) do nothing;

alter table app_auth enable row level security;
