"""Pure gold indicator math and entry policy (no I/O).

Gold uses true EMA200, Wilder ATR14, and closed UTC daily mid-price bars;
other assets keep EMA100 and the existing simple ATR14.

Conventions:
- ``wilder_atr`` takes candle-like objects with ``h``/``l``/``c``
  attributes (dicts with the same keys are also accepted).
- ``true_ema`` takes a sequence of closes and requires full history:
  it raises ``ValueError`` when ``len(closes) < period`` (fail closed —
  callers treat missing values as blocked).
- ``gold_entry_block_reason`` returns ``""`` when the direction is
  allowed by gold entry policy; otherwise a stable reason-code string.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

GOLD_DEFAULT_MAX_ATR_PCT = 3.5
FX_MAX_ATR_PCT = 2.5
GOLD_SELL_MIN_ADX = 25.0


def _candle_field(candle: Any, name: str) -> float:
    if isinstance(candle, Mapping):
        return float(candle[name])
    return float(getattr(candle, name))


def wilder_atr(candles: Sequence[Any], period: int = 14) -> float:
    """Wilder ATR: seed from the first ``period`` True Ranges, then smooth.

    Raises ``ValueError`` when fewer than ``period + 1`` candles are given
    (the first bar has no previous close, so ``period`` TRs need
    ``period + 1`` candles).
    """
    bars = list(candles)
    if len(bars) < period + 1:
        raise ValueError(
            f"wilder_atr needs at least {period + 1} candles, got {len(bars)}"
        )
    trs: list[float] = []
    trs.append(_candle_field(bars[0], "h") - _candle_field(bars[0], "l"))
    for i in range(1, len(bars)):
        h = _candle_field(bars[i], "h")
        low = _candle_field(bars[i], "l")
        prev_close = _candle_field(bars[i - 1], "c")
        trs.append(max(h - low, abs(h - prev_close), abs(low - prev_close)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def true_ema(closes: Sequence[float], period: int = 200) -> float:
    """Classic EMA with ``k = 2 / (period + 1)``, seeded from closes[0].

    Raises ``ValueError`` on insufficient history (fail closed).
    """
    values = [float(v) for v in closes]
    if len(values) < period:
        raise ValueError(
            f"true_ema needs at least {period} closes, got {len(values)}"
        )
    k = 2.0 / (period + 1)
    ema = values[0]
    for price in values[1:]:
        ema = price * k + ema * (1.0 - k)
    return ema


def _is_gold_asset(asset: str) -> bool:
    return str(asset or "").upper() == "XAUUSD"


def effective_max_atr_pct(settings: Any, asset: str) -> float:
    """Gold returns its own threshold; every other asset returns 2.5."""
    if not _is_gold_asset(asset):
        return FX_MAX_ATR_PCT
    return float(getattr(settings, "gold_max_atr_pct", GOLD_DEFAULT_MAX_ATR_PCT))


def gold_entry_block_reason(
    settings: Any,
    *,
    asset: str,
    direction: str,
    snapshot: Mapping[str, Any] | None,
) -> str:
    """Return ``""`` when allowed by gold entry policy, else a reason code.

    Check order: (1) disabled, (2) degraded feed, (3) insufficient
    history, (4) ATR threshold, (5) direction EMA trend, (6) SELL ADX,
    (7) BUY breakout. Non-gold assets always return ``""``.
    """
    if not _is_gold_asset(asset):
        return ""
    snap: Mapping[str, Any] = snapshot or {}

    threshold = effective_max_atr_pct(settings, asset)
    if threshold == 0:
        return "gold_trading_disabled"

    if snap.get("trusted") is False:
        return "gold_feed_degraded"
    if snap.get("ohlc_source") == "yahoo_degraded":
        return "gold_feed_degraded"

    required = ("ema_fast", "ema_slow", "adx", "atr_pct", "breakout_state")
    if any(snap.get(key) is None for key in required):
        return "gold_insufficient_history"

    atr_pct = float(snap["atr_pct"])
    if atr_pct > threshold:
        return "gold_atr_exceeded"

    side = str(direction or "").upper()
    ema_fast = float(snap["ema_fast"])
    ema_slow = float(snap["ema_slow"])
    if side == "SELL":
        if ema_fast >= ema_slow:
            return "gold_trend_not_bearish"
        if float(snap["adx"]) < GOLD_SELL_MIN_ADX:
            return "gold_trend_strength_lost"
        return ""
    if side == "BUY":
        if ema_fast <= ema_slow:
            return "gold_trend_not_bullish"
        if bool(getattr(settings, "gold_breakout_only", True)) and snap.get("breakout_state") not in (1, 2):
            return "breakout_invalidated"
        return ""
    return "gold_invalid_direction"
