-- 036_kill_expand_confirm.sql
-- Owner-confirmed risk-limit expansion (drawdown + kill-switch limits).
-- Run in Supabase SQL Editor (migrations are applied by hand).

-- WHY THIS EXISTS (prod 2026-09-14)
--   Drawdown 10.06% > 10.00% correctly fired the kill switch: the position
--   guard closed all 5 open positions (winners included) to cut exposure and
--   Gate 1 blocked every new order. Correct — but it left NO sanctioned way to
--   continue trading, and silently widening a risk limit is exactly the failure
--   mode a kill switch exists to prevent.
--
--   Expansion is therefore an explicit OWNER decision made on LINE:
--     breach → pending row in this table (nothing is widened yet)
--            → LINE prompt "Approve/Reject" (or /dd_ok, /dd_no)
--            → approve: write the new limits + lift pause + re-evaluate kill
--               reject : limits untouched, trading stays paused
--
--   A row here is only a REQUEST. Nothing in this table changes any limit by
--   itself; app/services/limit_expand.py does the write after confirmation.

create table if not exists kill_expand_requests (
    id           uuid primary key default uuid_generate_v4(),
    -- text (not uuid FK): the app runs on the pseudo-user "demo" and a uuid
    -- column would reject it (same gotcha as notifications.user_id).
    user_id      text not null default 'demo',
    status       text not null default 'pending',   -- pending | approved | rejected | expired
    trigger_type text not null default '',          -- drawdown,daily,weekly,monthly (csv)
    metric_value numeric(12, 4) not null default 0, -- worst current value (percent)
    limit_before numeric(12, 4) not null default 0,
    limit_after  numeric(12, 4) not null default 0,
    detail       jsonb not null default '{}'::jsonb,-- {triggers:[...], source, reason}
    requested_at timestamptz not null default now(),
    decided_at   timestamptz,
    decided_by   text not null default '',          -- line:user / line:group / api
    created_at   timestamptz not null default now()
);

create index if not exists idx_kill_expand_status
    on kill_expand_requests(status, requested_at desc);

-- RLS: read open (static frontend), writes via the service role only.
alter table kill_expand_requests enable row level security;

drop policy if exists "kill_expand_select_all" on kill_expand_requests;
create policy "kill_expand_select_all"
    on kill_expand_requests for select
    using (true);

drop policy if exists "kill_expand_write_all" on kill_expand_requests;
create policy "kill_expand_write_all"
    on kill_expand_requests for all
    using (true)
    with check (true);

-- notifications.type is a Postgres ENUM (migration 001) with a fixed value
-- list, so the confirmation prompt needs its OWN type. It is also what keeps
-- the prompt out of the risk_warning 30-minute cooldown: the breach notice is
-- throttled, but a pending Approve/Reject request must always be deliverable.
alter type notification_type add value if not exists 'limit_expand';
