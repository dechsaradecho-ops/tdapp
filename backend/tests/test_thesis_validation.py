"""Final thesis validation (P0-5).

A signal is generated against the market AT THAT MOMENT and stored as a row;
it fires up to ``SIGNAL_TTL_MIN`` (30) minutes later. ``execute_signal``
re-anchors the ENTRY to the live spot, but not the REASON the signal existed.
These tests pin the pure ``validate_thesis`` rules AND that ``execute_signal``
BLOCKS (never crashes) when the stored thesis no longer holds on a FRESH
snapshot — identically on the manual-approve and auto-trader paths.

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_thesis_validation.py -v
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core import thesis_validation as tv
from app.models.schemas import AppSettings
from app.services import execution
from app.integrations import quotes as quotes_mod
from app.integrations.brokers import PaperBroker
from tests.test_workers import FakeDatabase


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
def _snapshot(**over) -> dict:
    """A fresh, trend-confirming BUY-friendly snapshot by default."""
    base = {
        "asset": "EURUSD",
        "price": 1.1000,
        "ema_fast": 1.1050,
        "ema_slow": 1.0900,      # fast > slow → bull trend
        "adx": 30.0,
        "supertrend_dir": 1,      # up
        "rsi": 55.0,
        "macd_hist": 0.1,
        "price_change_pct_20": 1.0,
        "atr_pct": 1.0,           # < 2.5 → not high volatility
        "volatility_index": 10.0,
        "news_sentiment": 0.0,
        "high_impact_event": False,
        "breakout_state": 0,
        "breakout_level": 0.0,
    }
    base.update(over)
    return base


def _signal(**over) -> dict:
    base = {
        "id": "sig-1",
        "asset": "EURUSD",
        "direction": "BUY",
        "entry": 1.1000,
        "stop_loss": 1.0950,
        "take_profit": 1.1100,
        "confidence": 80.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # P0-5 baseline captured at signal creation (migration 045).
        # These tests model a POST-045 row: at creation the trend was bullish
        # and Supertrend agreed (+1).
        "baseline_supertrend_dir": 1,
        "baseline_macd_hist": 0.1,
        "baseline_ema_fast": 1.1050,
        "baseline_ema_slow": 1.0900,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# Pure validation — the happy path
# --------------------------------------------------------------------------
class TestValidThesis:
    def test_buy_thesis_holds(self):
        r = tv.validate_thesis(_signal(direction="BUY"), _snapshot())
        assert r.status == tv.STATUS_VALID
        assert r.ok is True
        assert r.error_codes == []
        assert r.reason == ""

    def test_sell_thesis_holds(self):
        snap = _snapshot(ema_fast=1.0900, ema_slow=1.1050, supertrend_dir=-1)
        r = tv.validate_thesis(_signal(direction="SELL"), snap)
        assert r.ok is True

    def test_no_signal_row_is_back_compat_valid(self):
        assert tv.validate_thesis(None, _snapshot()).ok is True
        assert tv.validate_thesis({}, _snapshot()).ok is True

    def test_snapshot_without_indicators_does_not_false_block(self):
        # A feed that did not compute EMAs/Supertrend for the asset must not be
        # read as a bear trend — all-zero trend fields ⇒ checks skipped.
        snap = _snapshot(ema_fast=0.0, ema_slow=0.0, supertrend_dir=0, adx=0.0)
        r = tv.validate_thesis(_signal(direction="BUY"), snap)
        assert r.ok is True

    def test_values_snapshot_is_populated(self):
        r = tv.validate_thesis(_signal(), _snapshot())
        assert r.values["direction"] == "BUY"
        assert r.values["supertrend_dir"] == 1
        assert r.values["asset"] == "EURUSD"


# --------------------------------------------------------------------------
# Pure validation — each machine-readable reject reason
# --------------------------------------------------------------------------
class TestTrendFlipped:
    def test_buy_against_down_trend_blocks(self):
        snap = _snapshot(ema_fast=1.0900, ema_slow=1.1050)   # fast < slow
        r = tv.validate_thesis(_signal(direction="BUY"), snap)
        assert r.status == tv.STATUS_INVALID
        assert "trend_flipped" in r.error_codes

    def test_sell_against_up_trend_blocks(self):
        snap = _snapshot(ema_fast=1.1050, ema_slow=1.0900)   # fast > slow
        r = tv.validate_thesis(_signal(direction="SELL"), snap)
        assert "trend_flipped" in r.error_codes


class TestSupertrendConfirmation:
    """P0-5 refinement: EMA is the primary thesis; Supertrend is CONFIRMATION.

    A single opposing Supertrend reading is a NON-BLOCKING warning
    (``supertrend_conflict`` from the start, ``supertrend_flipped`` when it
    agreed at creation and flipped later). Only when Supertrend AND MACD
    oppose TOGETHER does the order block (``thesis_corroboration_failed``).
    """

    def test_buy_with_down_supertrend_warns_but_does_not_block(self):
        snap = _snapshot(supertrend_dir=-1)
        sig = _signal(direction="BUY", baseline_supertrend_dir=-1)
        r = tv.validate_thesis(sig, snap)
        assert r.ok is True
        assert "supertrend_conflict" in [i.code for i in r.issues]
        assert "supertrend_flipped" not in r.error_codes
        assert r.error_codes == []

    def test_sell_with_up_supertrend_warns_but_does_not_block(self):
        # Bearish EMA snapshot (so a SELL is not regime-blocked); Supertrend
        # points UP (opposes the SELL) with a matching baseline ⇒ conflict.
        # MACD must AGREE with the sell (-0.1) or it would be a corroboration
        # failure (both confirming indicators opposing).
        snap = _snapshot(supertrend_dir=1, ema_fast=1.0900, ema_slow=1.1050,
                         macd_hist=-0.1)
        sig = _signal(direction="SELL", baseline_supertrend_dir=1)
        r = tv.validate_thesis(sig, snap)
        assert r.ok is True
        assert "supertrend_conflict" in [i.code for i in r.issues]

    def test_no_baseline_reads_as_conflict_not_flip(self):
        # A pre-045 signal has no baseline column ⇒ conflict (never a false
        # "flipped" blame).
        snap = _snapshot(supertrend_dir=-1)
        sig = _signal(direction="BUY", baseline_supertrend_dir=None,
                      baseline_macd_hist=None)
        r = tv.validate_thesis(sig, snap)
        codes = [i.code for i in r.issues]
        assert "supertrend_conflict" in codes
        assert "supertrend_flipped" not in codes

    def test_missing_baseline_columns_read_as_conflict(self):
        # Keys entirely absent (old row dict) behave the same as None.
        sig = _signal(direction="BUY")
        sig.pop("baseline_supertrend_dir")
        r = tv.validate_thesis(sig, _snapshot(supertrend_dir=-1))
        assert "supertrend_conflict" in [i.code for i in r.issues]

    def test_baseline_agreed_then_flip_is_supertrend_flipped(self):
        # At creation Supertrend agreed (baseline +1, BUY); now it opposes.
        sig = _signal(direction="BUY", baseline_supertrend_dir=1)
        r = tv.validate_thesis(sig, _snapshot(supertrend_dir=-1))
        codes = [i.code for i in r.issues]
        assert "supertrend_flipped" in codes
        assert "supertrend_conflict" not in codes
        assert r.ok is True                     # still non-blocking

    def test_baseline_opposed_stays_conflict(self):
        # At creation Supertrend already opposed (baseline -1, BUY).
        sig = _signal(direction="BUY", baseline_supertrend_dir=-1)
        r = tv.validate_thesis(sig, _snapshot(supertrend_dir=-1))
        codes = [i.code for i in r.issues]
        assert "supertrend_conflict" in codes
        assert "supertrend_flipped" not in codes

    def test_supertrend_and_macd_together_block(self):
        snap = _snapshot(supertrend_dir=-1, macd_hist=-0.2)
        r = tv.validate_thesis(_signal(direction="BUY"), snap)
        assert r.status == tv.STATUS_INVALID
        assert "thesis_corroboration_failed" in r.error_codes

    def test_supertrend_and_macd_together_block_for_sell(self):
        snap = _snapshot(supertrend_dir=1, macd_hist=0.2)
        r = tv.validate_thesis(_signal(direction="SELL"), snap)
        assert "thesis_corroboration_failed" in r.error_codes

    def test_macd_only_opposing_does_not_block(self):
        # MACD disagrees but Supertrend agrees ⇒ not a failed corroboration.
        snap = _snapshot(supertrend_dir=1, macd_hist=-0.2)
        r = tv.validate_thesis(_signal(direction="BUY"), snap)
        assert r.ok is True


class TestRegimeBlocked:
    def test_sideway_regime_blocks(self):
        snap = _snapshot(adx=10.0)                # adx < 20 → sideway
        r = tv.validate_thesis(_signal(direction="BUY"), snap)
        assert "regime_blocked" in r.error_codes

    def test_high_volatility_regime_blocks(self):
        snap = _snapshot(adx=30.0, atr_pct=4.0)   # adx≥20, atr>2.5
        r = tv.validate_thesis(_signal(direction="BUY"), snap)
        assert "regime_blocked" in r.error_codes

    def test_news_regime_blocks(self):
        snap = _snapshot(high_impact_event=True)
        r = tv.validate_thesis(_signal(direction="BUY"), snap)
        assert "regime_blocked" in r.error_codes

    def test_bear_regime_blocks_a_buy(self):
        snap = _snapshot(ema_fast=1.0900, ema_slow=1.1050, adx=30.0)
        r = tv.validate_thesis(_signal(direction="BUY"), snap)
        assert "regime_blocked" in r.error_codes


class TestBreakoutInvalidated:
    def test_gold_buy_without_breakout_structure_blocks(self):
        snap = _snapshot(asset="XAUUSD", breakout_state=0, breakout_level=2000.0)
        r = tv.validate_thesis(_signal(asset="XAUUSD", direction="BUY"), snap)
        assert "breakout_invalidated" in r.error_codes

    def test_gold_sell_with_live_up_breakout_blocks(self):
        snap = _snapshot(asset="XAUUSD", breakout_state=2, breakout_level=2000.0)
        r = tv.validate_thesis(_signal(asset="XAUUSD", direction="SELL"), snap)
        assert "breakout_invalidated" in r.error_codes

    def test_gold_buy_with_breakout_is_ok(self):
        snap = _snapshot(asset="XAUUSD", breakout_state=2, breakout_level=2000.0)
        r = tv.validate_thesis(_signal(asset="XAUUSD", direction="BUY"), snap)
        assert r.ok is True

    def test_non_gold_breakout_fields_are_ignored(self):
        # FX snapshot always reports breakout 0.0/0.0 — must NOT block.
        snap = _snapshot(asset="EURUSD", breakout_state=0, breakout_level=0.0)
        r = tv.validate_thesis(_signal(asset="EURUSD", direction="BUY"), snap)
        assert r.ok is True


class TestNewsStateChanged:
    def test_high_impact_event_flagged_now_blocks(self):
        snap = _snapshot(high_impact_event=True)
        r = tv.validate_thesis(_signal(direction="BUY"), snap)
        codes = r.error_codes
        assert "news_state_changed" in codes
        # Umbrella code is added when news is the ONLY problem.
        assert "signal_thesis_invalidated" in codes


class TestSignalExpired:
    def test_old_signal_expires(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        r = tv.validate_thesis(_signal(created_at=old), _snapshot(),
                               max_age_min=30)
        assert "signal_expired" in r.error_codes

    def test_fresh_signal_does_not_expire(self):
        r = tv.validate_thesis(_signal(), _snapshot(), max_age_min=30)
        assert r.ok is True

    def test_no_max_age_skips_expiry(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=999)).isoformat()
        r = tv.validate_thesis(_signal(created_at=old), _snapshot())
        assert "signal_expired" not in r.error_codes


class TestSnapshotUnavailable:
    def test_none_snapshot_blocks_fail_closed(self):
        r = tv.validate_thesis(_signal(), None)
        assert r.status == tv.STATUS_UNKNOWN
        assert r.ok is False
        assert r.reason == "latest_snapshot_unavailable"

    def test_empty_snapshot_blocks_fail_closed(self):
        r = tv.validate_thesis(_signal(), {})
        assert r.ok is False
        assert r.reason == "latest_snapshot_unavailable"

    def test_snapshot_without_asset_blocks(self):
        r = tv.validate_thesis(_signal(), {"ema_fast": 1.1, "ema_slow": 1.0})
        assert r.reason == "latest_snapshot_unavailable"


class TestSummaryAndDict:
    def test_summary_valid(self):
        assert "VALID" in tv.validate_thesis(_signal(), _snapshot()).summary()

    def test_summary_unknown(self):
        assert "UNKNOWN" in tv.validate_thesis(_signal(), None).summary()

    def test_as_dict_shape(self):
        d = tv.validate_thesis(_signal(direction="BUY"),
                               _snapshot(ema_fast=1.0900,
                                         ema_slow=1.1050)).as_dict()
        assert d["status"] == tv.STATUS_INVALID
        assert d["ok"] is False
        assert d["reason"]
        assert isinstance(d["issues"], list) and d["issues"]


# --------------------------------------------------------------------------
# execute_signal seam (fail-closed, never crashes, manual == auto)
# --------------------------------------------------------------------------
class _RecordingNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, user_id, ntype, message, critical=None,
                     quick_reply=None):
        self.sent.append({"type": ntype, "message": message})


class _RecordingBroker(PaperBroker):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.orders = []

    async def place_order(self, req):
        self.orders.append(req)
        return await super().place_order(req)


def _patch_snapshots(monkeypatch, snaps):
    async def _fake(assets, concurrency=5, ttl=None):
        return {a: snaps for a in (assets or [])} if snaps else {}
    monkeypatch.setattr(quotes_mod, "fetch_all_snapshots", _fake)


class TestExecuteSignalThesisSeam:
    def test_inflated_signal_blocks_and_never_reaches_broker(self, monkeypatch):
        # EMA flipped (primary thesis) ⇒ BLOCKS, never reaches the broker.
        _patch_snapshots(monkeypatch, _snapshot(ema_fast=1.0900,
                                                ema_slow=1.1050))
        db = FakeDatabase()
        broker = _RecordingBroker()
        notifier = _RecordingNotifier()
        report = asyncio.run(execution.execute_signal(
            db, broker, notifier, AppSettings(capital=10_000, risk_per_trade_pct=1.0),
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.1000, stop_loss=1.0950, take_profit=1.1100,
            confidence=90.0, opportunity=90.0, signal_id="sig-1",
            source="approved", signal_row=_signal(direction="BUY")))
        assert report.allowed is False
        assert report.thesis_error == "trend_flipped"
        assert any("thesis_error" in r for r in report.rejects)
        assert broker.orders == []

    def test_supertrend_only_conflict_does_not_block(self, monkeypatch):
        # Supertrend alone opposes (EMA still agrees) ⇒ order FIRES.
        _patch_snapshots(monkeypatch, _snapshot(supertrend_dir=-1,
                                                macd_hist=0.1))
        db = FakeDatabase()
        broker = _RecordingBroker()
        notifier = _RecordingNotifier()
        report = asyncio.run(execution.execute_signal(
            db, broker, notifier, AppSettings(capital=10_000, risk_per_trade_pct=1.0),
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.1000, stop_loss=1.0950, take_profit=1.1100,
            confidence=90.0, opportunity=90.0, signal_id="sig-1",
            source="approved", signal_row=_signal(direction="BUY")))
        assert report.allowed is True
        assert report.thesis_error is None
        assert len(broker.orders) == 1

    def test_unavailable_snapshot_blocks_fail_closed(self, monkeypatch):
        _patch_snapshots(monkeypatch, None)      # empty → no snapshot
        db = FakeDatabase()
        broker = _RecordingBroker()
        notifier = _RecordingNotifier()
        report = asyncio.run(execution.execute_signal(
            db, broker, notifier, AppSettings(capital=10_000, risk_per_trade_pct=1.0),
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.1000, stop_loss=1.0950, take_profit=1.1100,
            confidence=90.0, opportunity=90.0, signal_id="sig-1",
            source="approved", signal_row=_signal(direction="BUY")))
        assert report.allowed is False
        assert report.thesis_error == "latest_snapshot_unavailable"
        assert broker.orders == []

    def test_no_signal_row_skips_thesis_check(self, monkeypatch):
        # extended-open passes no row → thesis check is skipped entirely, and
        # the (unavailable) snapshot must NOT block it.
        _patch_snapshots(monkeypatch, None)
        db = FakeDatabase()
        broker = _RecordingBroker()
        notifier = _RecordingNotifier()
        report = asyncio.run(execution.execute_signal(
            db, broker, notifier, AppSettings(capital=10_000, risk_per_trade_pct=1.0),
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.1000, stop_loss=1.0950, take_profit=1.1100,
            confidence=90.0, opportunity=90.0, signal_id="plan-1",
            source="extended_open"))
        assert report.thesis_error is None

    @pytest.mark.parametrize("source", ["approved", "auto"])
    def test_manual_and_auto_share_the_same_thesis_block(self, monkeypatch,
                                                         source):
        _patch_snapshots(monkeypatch, _snapshot(ema_fast=1.0900,
                                                ema_slow=1.1050))
        db = FakeDatabase()
        broker = _RecordingBroker()
        notifier = _RecordingNotifier()
        report = asyncio.run(execution.execute_signal(
            db, broker, notifier, AppSettings(capital=10_000, risk_per_trade_pct=1.0),
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.1000, stop_loss=1.0950, take_profit=1.1100,
            confidence=90.0, opportunity=90.0, signal_id="sig-1",
            source=source, signal_row=_signal(direction="BUY")))
        assert report.allowed is False
        assert report.thesis_error == "trend_flipped"
        assert broker.orders == []
