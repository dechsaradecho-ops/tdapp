-- 018_line_targets.sql
-- LINE group/room notification targets + AI chat support.
-- Run in Supabase SQL Editor (user runs migrations manually).

-- 1) line_targets — group/room chats auto-registered by the LINE webhook.
--    The first time the bot is added to a group (or mentioned there), the
--    webhook stores the groupId here; every notification dispatch then
--    pushes alerts to the group in addition to personal line_users rows.
create table if not exists line_targets (
    id                   uuid primary key default uuid_generate_v4(),
    target_id            text not null unique,          -- groupId / roomId (C...)
    target_type          text not null default 'group', -- group | room
    notification_enabled boolean not null default true,
    last_seen_at         timestamptz,                   -- latest webhook event from this chat
    created_at           timestamptz not null default now()
);

-- For tables created before 018 added last_seen_at (idempotent backfill)
alter table line_targets add column if not exists last_seen_at timestamptz;

-- 2) RLS: read open to anon (static frontend), writes via service role only
alter table line_targets enable row level security;

drop policy if exists "line_targets_select_all" on line_targets;
create policy "line_targets_select_all"
    on line_targets for select
    using (true);

drop policy if exists "line_targets_insert_service" on line_targets;
create policy "line_targets_insert_service"
    on line_targets for insert
    with check (true);

drop policy if exists "line_targets_update_service" on line_targets;
create policy "line_targets_update_service"
    on line_targets for update
    using (true)
    with check (true);
