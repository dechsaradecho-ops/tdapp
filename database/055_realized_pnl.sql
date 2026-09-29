-- 055_realized_pnl.sql
-- Bank partial-close PnL on the journal row so a position closed in slices
-- reports its TOTAL, not just the last slice.
--
-- WHY (prod 2026-09-29 PAPER-000120): 0.02 SELL GBPNZD banked +3.05 at TP1,
-- then the remaining 0.01 hit trailing SL for +1.62 — but the closed row
-- showed volume 0.01/0.02 with pnl 1.62 only. The TP1 slice lived solely in
-- signal_logs (7-day TTL): once purged, the banked money is unauditable and
-- win-rate/PnL aggregates undercount it.
--
-- `realized_pnl` accumulates every banked slice (TP1 + smart-exit scale-outs);
-- close_trade_rows() writes pnl = realized + final slice and volume back to
-- the initial size, so a fully-closed position always reads N/N with the
-- whole-trade PnL. Rows that never partial-close keep realized_pnl = 0 and
-- behave byte-identically to before.

alter table paper_trades
    add column if not exists realized_pnl numeric(18, 2) not null default 0;

comment on column paper_trades.realized_pnl is
    'USD PnL already banked by partial closes (TP1 / smart-exit scale-outs). '
    'The final whole close adds its slice on top: journal pnl = realized + final.';
