"""Unit tests for app/core/gold_policy.py — pure math/policy, no I/O.

Run from backend/: pytest tests/test_gold_policy.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.gold_policy import (
    effective_max_atr_pct,
    gold_entry_block_reason,
    true_ema,
    wilder_atr,
)


class Candle:
    def __init__(self, h: float, l: float, c: float):
        self.h = h
        self.l = l
        self.c = c


TR_CANDLE_FIXTURE = [
    Candle(4030, 3990, 4010), Candle(4045, 4000, 4035),
    Candle(4038, 4015, 4020), Candle(4060, 4018, 4050),
    Candle(4070, 4040, 4045), Candle(4055, 4025, 4030),
    Candle(4080, 4030, 4070), Candle(4090, 4060, 4065),
    Candle(4075, 4045, 4050), Candle(4100, 4050, 4090),
    Candle(4110, 4080, 4085), Candle(4095, 4060, 4070),
    Candle(4120, 4070, 4110), Candle(4130, 4100, 4115),
    Candle(4115, 4085, 4090), Candle(4140, 4090, 4130),
]
EXPECTED_WILDER_ATR = 37.279518950437314

CLOSES_200 = [3800.0 + ((i * 37) % 251) - 125 for i in range(200)]
EXPECTED_EMA200 = 3781.91203663492


def test_wilder_atr_matches_known_fixture():
    assert wilder_atr(TR_CANDLE_FIXTURE, period=14) == pytest.approx(EXPECTED_WILDER_ATR)


def test_true_ema200_requires_full_history():
    assert true_ema(CLOSES_200, period=200) == pytest.approx(EXPECTED_EMA200)


def test_gold_threshold_is_separate_from_fx():
    s = SimpleNamespace(gold_max_atr_pct=3.5)
    assert effective_max_atr_pct(s, "XAUUSD") == 3.5
    assert effective_max_atr_pct(s, "EURUSD") == 2.5


def test_gold_sell_trend_and_buy_breakout_policy():
    s = SimpleNamespace(gold_max_atr_pct=3.5, gold_breakout_only=True)
    bearish = {"ema_fast": 3976, "ema_slow": 4212, "adx": 36,
               "atr_pct": 2.47, "breakout_state": 0}
    assert gold_entry_block_reason(s, asset="XAUUSD", direction="SELL", snapshot=bearish) == ""
    assert gold_entry_block_reason(s, asset="XAUUSD", direction="BUY", snapshot=bearish) != ""
    disabled = SimpleNamespace(gold_max_atr_pct=0, gold_breakout_only=True)
    assert "gold_trading_disabled" in gold_entry_block_reason(
        disabled, asset="XAUUSD", direction="SELL", snapshot=bearish)


# ---- Additional edge-case coverage ----------------------------------------

def test_wilder_atr_accepts_dict_candles():
    dicts = [{"h": c.h, "l": c.l, "c": c.c} for c in TR_CANDLE_FIXTURE]
    assert wilder_atr(dicts, period=14) == pytest.approx(EXPECTED_WILDER_ATR)


def test_wilder_atr_insufficient_history_raises():
    with pytest.raises(ValueError):
        wilder_atr(TR_CANDLE_FIXTURE[:5], period=14)


def test_true_ema_insufficient_history_raises():
    with pytest.raises(ValueError):
        true_ema(CLOSES_200[:100], period=200)


def test_effective_max_atr_pct_missing_field_falls_back():
    assert effective_max_atr_pct(SimpleNamespace(), "XAUUSD") == 3.5
    assert effective_max_atr_pct(SimpleNamespace(), "EURUSD") == 2.5


def test_non_gold_asset_always_allowed():
    s = SimpleNamespace(gold_max_atr_pct=0, gold_breakout_only=True)
    assert gold_entry_block_reason(s, asset="EURUSD", direction="SELL", snapshot={}) == ""


def test_gold_atr_exceeded():
    s = SimpleNamespace(gold_max_atr_pct=3.5, gold_breakout_only=True)
    snap = {"ema_fast": 3976, "ema_slow": 4212, "adx": 36,
            "atr_pct": 4.0, "breakout_state": 0}
    assert gold_entry_block_reason(s, asset="XAUUSD", direction="SELL", snapshot=snap) == "gold_atr_exceeded"


def test_gold_sell_blocked_when_not_bearish_or_weak_adx():
    s = SimpleNamespace(gold_max_atr_pct=3.5, gold_breakout_only=True)
    bullish = {"ema_fast": 4212, "ema_slow": 3976, "adx": 36,
               "atr_pct": 2.0, "breakout_state": 0}
    assert gold_entry_block_reason(s, asset="XAUUSD", direction="SELL", snapshot=bullish) == "gold_trend_not_bearish"
    weak = {"ema_fast": 3976, "ema_slow": 4212, "adx": 20,
            "atr_pct": 2.0, "breakout_state": 0}
    assert gold_entry_block_reason(s, asset="XAUUSD", direction="SELL", snapshot=weak) == "gold_trend_strength_lost"


def test_gold_buy_breakout_only_enforced():
    s = SimpleNamespace(gold_max_atr_pct=3.5, gold_breakout_only=True)
    bullish_no_breakout = {"ema_fast": 4212, "ema_slow": 3976, "adx": 30,
                           "atr_pct": 2.0, "breakout_state": 0}
    assert gold_entry_block_reason(
        s, asset="XAUUSD", direction="BUY", snapshot=bullish_no_breakout) == "breakout_invalidated"
    bullish_breakout = dict(bullish_no_breakout, breakout_state=1)
    assert gold_entry_block_reason(
        s, asset="XAUUSD", direction="BUY", snapshot=bullish_breakout) == ""
    relaxed = SimpleNamespace(gold_max_atr_pct=3.5, gold_breakout_only=False)
    assert gold_entry_block_reason(
        relaxed, asset="XAUUSD", direction="BUY", snapshot=bullish_no_breakout) == ""


def test_gold_degraded_feed_and_insufficient_history():
    s = SimpleNamespace(gold_max_atr_pct=3.5, gold_breakout_only=True)
    base = {"ema_fast": 3976, "ema_slow": 4212, "adx": 36,
            "atr_pct": 2.0, "breakout_state": 0}
    assert gold_entry_block_reason(
        s, asset="XAUUSD", direction="SELL",
        snapshot=dict(base, trusted=False)) == "gold_feed_degraded"
    assert gold_entry_block_reason(
        s, asset="XAUUSD", direction="SELL",
        snapshot=dict(base, ohlc_source="yahoo_degraded")) == "gold_feed_degraded"
    assert gold_entry_block_reason(
        s, asset="XAUUSD", direction="SELL", snapshot={}) == "gold_insufficient_history"
