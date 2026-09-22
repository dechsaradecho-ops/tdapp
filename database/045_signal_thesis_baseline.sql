-- 045_signal_thesis_baseline.sql
-- P0-5 refinement: store the INDICATOR VALUES captured when a signal is
-- created so the final thesis re-validation (app/core/thesis_validation.py)
-- can tell WHY a confirmation indicator disagrees at execution time.
--
-- Design (owner-approved):
--   * EMA remains the PRIMARY thesis — no scanner change to direction logic.
--   * Supertrend is CONFIRMATION only (NOT a sole veto). The current
--     quotes._supertrend_dir is a coarse SMA-band heuristic with no backtest
--     confirmation as a hard gate, so a single opposing reading must not kill
--     a trade. Without a baseline the gate could only ever say "Supertrend
--     opposes now" and had to BLOCK (the systematic USDJPY/GBPNZD veto).
--   * With these columns validate_thesis compares fresh vs creation values and
--     emits the precise, non-blocking code:
--         baseline_supertrend_dir AGREED  & now opposes -> supertrend_flipped
--         baseline_supertrend_dir OPPOSED & now opposes -> supertrend_conflict
--     (a genuine state change vs a from-the-start disagreement).
--   * Blocking now happens only when EMA flips (trend_flipped) or when BOTH
--     Supertrend AND MACD oppose together (thesis_corroboration_failed).
--
-- Nullable with NO default: pre-existing rows keep NULL, which the pure gate
-- reads as "no baseline" -> supertrend_conflict (never a false flip). Those
-- rows are also short-lived anyway (SIGNAL_TTL_MIN = 30) and are expired on
-- deploy by execution.expire_unbaselined_pending_signals().

alter table signals
  add column if not exists baseline_supertrend_dir integer;
alter table signals
  add column if not exists baseline_macd_hist double precision;
alter table signals
  add column if not exists baseline_ema_fast double precision;
alter table signals
  add column if not exists baseline_ema_slow double precision;

comment on column signals.baseline_supertrend_dir is
  'P0-5: Supertrend direction (+1/-1, 0=unknown) at signal creation. Lets the thesis gate distinguish supertrend_flipped (agreed before) from supertrend_conflict (opposed from the start). NULL => no baseline (pre-045 row).';

comment on column signals.baseline_macd_hist is
  'P0-5: MACD histogram at signal creation (stored for corroboration/telemetry; EMA is the primary thesis and Supertrend+MACD together form the blocking confirmation).';

comment on column signals.baseline_ema_fast is
  'P0-5: EMA-fast (EMA50) at signal creation, for trend-flip context/telemetry.';

comment on column signals.baseline_ema_slow is
  'P0-5: EMA-slow (EMA100/200) at signal creation, for trend-flip context/telemetry.';
