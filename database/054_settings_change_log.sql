-- 054_settings_change_log.sql
--
-- Audit trail for Settings-page changes: WHO changed WHAT from WHICH value
-- to WHICH value, shown in the Logs menu (settings tab).
--
-- Prod 2026-09-28: the owner raised max_drawdown 10 → 25 but had no way to
-- verify WHEN it changed or WHAT it was before — the alerts quoting the old
-- 10.00% limit could not be reconciled against the row that said 25.0.
-- Every successful write path (PUT /api/settings, preset apply, reset,
-- limit-expansion approve) now inserts one row here with the per-field
-- old→new diff. Read-only for the UI; never blocks a save (the writer
-- swallows errors — a missing migration must not break Settings saves).

create table if not exists settings_change_logs (
    id           uuid primary key default uuid_generate_v4(),
    -- who/what triggered it: ui | preset:<profile> | reset | limit_expand
    source       text not null default 'ui',
    -- [{field, old, new}] — only fields whose value actually changed
    changes      jsonb not null default '[]'::jsonb,
    -- human-readable one-liner, e.g. "max_drawdown 10 → 25 (+2 fields)"
    summary      text not null default '',
    created_at   timestamptz not null default now()
);
create index if not exists idx_settings_change_logs_created
    on settings_change_logs(created_at desc);

-- RLS: read open (static frontend), writes via the service role only.
alter table settings_change_logs enable row level security;

drop policy if exists "settings_change_select_all" on settings_change_logs;
create policy "settings_change_select_all"
    on settings_change_logs for select
    using (true);

drop policy if exists "settings_change_write_all" on settings_change_logs;
create policy "settings_change_write_all"
    on settings_change_logs for all
    using (true)
    with check (true);
