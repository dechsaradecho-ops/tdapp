-- 047_partial_done.sql
-- Persist the TP1 / smart-exit partial-close state on paper_trades.
--
-- WHY: position_guard.manage_position() and the smart-exit path both call
--   db.update("paper_trades", row_id, {"partial_done": True})
-- after a successful partial close, and rehydrate_book() reads
--   row.get("partial_done")
-- to carry the flag across restarts. The column never existed, so:
--   * the update was rejected by PostgREST (unknown column) and swallowed,
--   * rehydrate always restored partial_done=False,
--   * every worker restart re-fired TP1 (prod 2026-09-23: AUDNZD PAPER-000080
--     partial-closed 3 times at 01:05 / 03:00 / 03:15).
--
-- The reduced volume is also persisted now (see position_guard) so the
-- monitor shows the remaining size after a scale-out instead of the original.

alter table paper_trades
    add column if not exists partial_done boolean not null default false;

comment on column paper_trades.partial_done is
    'True once a TP1 / smart-exit partial close has fired for this position; '
    'prevents re-firing after a worker restart (rehydrate_book restores it).';
