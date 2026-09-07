-- ============================================================
-- Migration 021 — SL/TP move tracking (monitor move badge)
-- Run in Supabase SQL Editor (user runs migrations manually).
--
-- WHY: the position guard moves the SL (breakeven → trailing) on the
-- broker book, but the paper_trades row kept the ORIGINAL stop_loss —
-- so the monitor page showed the stale SL and users could not see that
-- the position was already protected / trailing. These columns persist
-- the move metadata so the monitor can badge SL/TP cells that changed
-- after entry and tooltip the details (original → current, when, why).
-- ============================================================

alter table paper_trades
    add column if not exists initial_stop_loss   numeric(18, 5),
    add column if not exists initial_take_profit numeric(18, 5),
    add column if not exists sl_moved_at         timestamptz,
    add column if not exists sl_move_reason      text,
    add column if not exists tp_moved_at         timestamptz,
    add column if not exists tp_move_reason      text;
