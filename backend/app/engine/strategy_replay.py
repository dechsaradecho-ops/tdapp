"""Historical replay of the LIVE signal generator, for barrier labeling.

WHY THIS EXISTS (owner 2026-10-06)
-----------------------------------
The platform had no way to answer "does this signal have an edge, and at what
stop width?". The evidence for that gap:

  * ``signals`` spans 11 days and joins to 25 usable outcomes, 95% CI on the
    win rate [20.8%, 59.2%] — statistically indistinguishable from a coin
  * ``baseline_ema_fast`` and ``baseline_macd_hist`` agreed with the trade
    direction in 25 of 25 rows — ZERO variance, so no model can learn from
    them as recorded
  * 44 closed trades, avg −0.239R; the best winner ever was +1.06R, so the
    1.5R target has never once been touched

Collecting 5,000 forward samples at ~2 trades/day would take years, and the
account cannot survive the wait. So this module replays the existing signal
generator over two years of daily candles instead. Same indicators, same
scorer, same confidence function — the LIVE code path, fed history.

THE HONEST CAVEATS (read before trusting any number it produces)
---------------------------------------------------------------
1. **Daily bars only.** The live feed is daily too (``interval=1d``), so the
   indicator math matches — but the live exit stack (partial close, trailing
   ladder, breakeven, ``no_behind_min_days``) acts on intrabar ticks this
   replay cannot see. Results describe the SIGNAL, not the live P&L.
2. **No news or sentiment.** ``news_sentiment`` is 0 and
   ``high_impact_event`` is False for every replayed bar because the DB only
   keeps 7 days of news. Live confidence carries a −30 news penalty, so
   replayed confidence is systematically HIGHER than live. Every WR reported
   here is therefore optimistic; the study prints this next to the number
   rather than in a footnote.
3. **Direction is taken from the EMA exactly as ``market_scanner`` line ~344
   does it**, not from ``_resolve_direction``. Mirroring the live path is the
   point — a prettier replay that diverges from production measures nothing.
4. **The replay ignores portfolio state**: correlation, open-position caps,
   drawdown throttling and the news gate are all absent, so a real session
   would have traded a SUBSET of these events. Treat n as an upper bound.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine, regime_of
from app.engine.triple_barrier import LabelledEvent

__all__ = ["REPLAY_CAVEATS", "replay_asset", "replay", "event_features"]

#: Printed verbatim by the study script. Kept here (not in the script) so the
#: caveats travel with the code that produces the numbers.
REPLAY_CAVEATS = (
    "daily bars only (no intrabar ticks)",
    "news/sentiment absent -> replayed confidence is OPTIMISTIC vs live",
    "direction from EMA (mirrors market_scanner)",
    "no portfolio state (correlation, open caps, DD throttle) -> n is an UPPER bound",
)

#: Bars of history the indicators need before a snapshot is meaningful.
#: ``snapshot_from_candles`` computes EMA50 and EMA100, so 100 bars is the
#: hard floor; 200 gives both EMAs room to converge off their seed value.
DEFAULT_WARMUP = 200


def _warm_snapshot(asset: str, window: Sequence[Any]) -> Optional[IndicatorSnapshot]:
    """candles -> IndicatorSnapshot via the LIVE indicator code.

    Deliberately calls ``quotes.snapshot_from_candles`` rather than
    recomputing anything: if the replay ever computes indicators its own way,
    the study silently stops describing the deployed strategy and starts
    describing a different one. That is the failure mode this indirection
    exists to prevent.
    """
    from app.integrations.quotes import snapshot_from_candles
    snap = snapshot_from_candles(asset, list(window), news_sentiment=0.0,
                                 high_impact_event=False)
    if not snap or not snap.get("price"):
        return None
    try:
        return IndicatorSnapshot(**snap)
    except TypeError:
        return None


def event_features(ind: IndicatorSnapshot, direction: str) -> dict[str, float]:
    """Variance-bearing snapshot values, stored alongside every event.

    Chosen because the two features currently persisted on ``signals``
    (EMA spread, MACD sign) are ~constant among executed trades — a model
    trained on constant features learns nothing. These all move:

      ``adx``                  trend strength (0 chop → 50+ trending)
      ``rsi``                  momentum position
      ``macd_hist``            momentum magnitude, not just sign
      ``atr_pct``              volatility regime
      ``volatility_index``     normalized ATR
      ``chg20``                20-bar momentum
      ``ema_gap_atr``          distance to the slow EMA in ATR units — the
                                trend "room left", comparable across pairs
      ``st_agree``             +1 Supertrend agrees with the side, else −1
      ``ema_agree``            +1 EMA agrees with the side, else −1
      ``macd_agree``           +1 MACD agrees with the side, else −1
    """
    want = 1 if direction.upper() == "BUY" else -1
    atr_price = (ind.price or 0) * (ind.atr_pct or 0) / 100.0
    gap_atr = ((ind.price - ind.ema_slow) / atr_price
               if atr_price > 0 and ind.ema_slow else 0.0)
    return {
        "adx": float(ind.adx or 0),
        "rsi": float(ind.rsi or 0),
        "macd_hist": float(ind.macd_hist or 0),
        "atr_pct": float(ind.atr_pct or 0),
        "volatility_index": float(ind.volatility_index or 0),
        "chg20": float(ind.price_change_pct_20 or 0),
        "ema_gap_atr": round(float(gap_atr), 3),
        "st_agree": 1.0 if (ind.supertrend_dir or 0) == want else -1.0,
        "ema_agree": 1.0 if ((ind.ema_fast > ind.ema_slow) == (want > 0)) else -1.0,
        "macd_agree": 1.0 if ((ind.macd_hist or 0) > 0) == (want > 0) else -1.0,
        "regime": str(regime_of(ind) or ""),
    }


def replay_asset(asset: str, candles: Sequence[Any], *,
                 warmup: int = DEFAULT_WARMUP,
                 cooldown_bars: int = 5,
                 engine: Optional[StrategyEngine] = None) -> list[LabelledEvent]:
    """Walk one asset's history and emit every candidate signal event.

    Every emitted event carries ``opportunity`` / ``confidence`` /
    ``features`` but is NOT filtered by them — the caller sweeps the
    thresholds afterwards, which is only possible because nothing is dropped
    here. ``cooldown_bars`` supplies the decorrelation instead: without it a
    persistent trend emits a "new" signal every single bar, and the same
    setup would be counted a hundred times, inflating n and flattering every
    statistic. A flat cooldown is deliberately threshold-independent so
    sweeping the gate cannot change which bars are eligible.
    """
    eng = engine or StrategyEngine()
    bars = list(candles or [])
    warm = max(int(warmup or 0), 2)
    cool = max(int(cooldown_bars or 0), 0)
    events: list[LabelledEvent] = []
    last_idx = -10 ** 9

    for i in range(warm - 1, len(bars)):
        if cool and (i - last_idx) < cool:
            continue
        window = bars[max(0, i - warm + 1):i + 1]
        if len(window) < warm:
            continue
        ind = _warm_snapshot(asset, window)
        if ind is None or not ind.price or not ind.atr_pct:
            continue
        # Direction exactly as market_scanner picks it (EMA spread).
        direction = "BUY" if ind.ema_fast > ind.ema_slow else "SELL"
        try:
            opp = eng.opportunity_score(ind, direction=direction)
        except Exception:
            continue
        try:
            opp = opp.model_copy(update={
                "confidence": eng.evidence_confidence(opp.score, ind, direction)[0]})
        except Exception:
            pass
        events.append(LabelledEvent(
            asset=asset, direction=direction, bar_index=i,
            entry=float(ind.price), atr_pct=float(ind.atr_pct),
            opportunity=float(getattr(opp, "score", 0) or 0),
            confidence=float(getattr(opp, "confidence", 0) or 0),
            future=bars[i + 1:],
            features=event_features(ind, direction),
        ))
        last_idx = i
    return events


def replay(series_by_asset: dict[str, Sequence[Any]], *,
           warmup: int = DEFAULT_WARMUP,
           cooldown_bars: int = 5) -> list[LabelledEvent]:
    """Replay a whole portfolio, sharing one engine instance."""
    eng = StrategyEngine()
    out: list[LabelledEvent] = []
    for asset in sorted(series_by_asset or {}):
        try:
            out.extend(replay_asset(asset, series_by_asset[asset],
                                    warmup=warmup, cooldown_bars=cooldown_bars,
                                    engine=eng))
        except Exception:
            # One bad series must not abort a 9-asset study.
            continue
    return out
