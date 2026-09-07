-- ============================================================
-- Migration 022 — Fix LINE notifications for trade_opened / trade_closed
-- Run in Supabase SQL Editor (supplements 001/020)
--
-- WHY (prod 2026-09-07): trade_opened / trade_closed / daily_digest never
-- reached LINE. Root causes, all silent:
--   1. notifications.user_id is a uuid FK but the app runs on the
--      pseudo-user "demo" → PostgREST rejected EVERY queued row with an
--      invalid-uuid error that Database.insert swallows → the dispatch
--      worker had nothing to send.
--   2. dispatch_pending skipped rows with an empty user_id ("broadcast").
--   3. push_line filtered line_users by the same pseudo uuid → the select
--      failed silently → personal chats never received anything.
-- The backend now queues with user_id = NULL (retry-once in
-- queue_notification) and pushes to every enabled chat. This migration
-- makes the schema match that contract:
--   • user_id becomes nullable (001 had it nullable already — re-asserted
--     here for clarity; the FK stays for real user rows)
--   • service_role may insert/select/update notifications (RLS: 001 only
--     created an auth.uid() policy, so worker inserts were ALSO denied)
--   • 'daily_digest' joins the notification_type enum (daily_digest worker
--     queued a type that 001 never allowed → second silent insert failure)
-- ============================================================

-- ---------- notifications.user_id nullable (idempotent re-assert) ----------
alter table notifications alter column user_id drop not null;

-- ---------- RLS: service_role writes/reads for the backend workers ----------
drop policy if exists "notifications_service_all" on notifications;
create policy "notifications_service_all"
  on notifications
  for all
  to service_role
  using (true)
  with check (true);

-- ---------- daily_digest joins the type enum ----------
alter type notification_type add value if not exists 'daily_digest';
