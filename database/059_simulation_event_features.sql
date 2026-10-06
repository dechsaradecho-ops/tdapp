-- 059_simulation_event_features.sql
-- Feature columns on simulation_events, for the meta-filter (owner 2026-10-06).
--
-- WHY
-- ---
-- The replay computes variance-bearing features per event (adx, rsi, momentum,
-- volatility regime, distance-to-trend, agreement flags) but never stored them
-- — the verdict only kept the label. A model that predicts "will this signal
-- go nowhere" needs the features AND the outcome side by side, so they are
-- persisted here from now on. Old runs have NULLs and are simply excluded
-- from training; nothing is backfilled or guessed.
--
-- All columns nullable on purpose: a run written before this migration (or a
-- row from a partially-failed persist) must stay readable, and the trainer
-- skips featureless rows instead of failing the whole job.

alter table simulation_events
    add column if not exists adx numeric,
    add column if not exists rsi numeric,
    add column if not exists macd_hist numeric,
    add column if not exists volatility_index numeric,
    add column if not exists chg20 numeric,
    add column if not exists ema_gap_atr numeric,
    add column if not exists st_agree numeric,
    add column if not exists ema_agree numeric,
    add column if not exists macd_agree numeric;

-- No new index: the trainer reads "all featured rows of a run, oldest first",
-- which migration 057's (run_id, seq) index already covers.
