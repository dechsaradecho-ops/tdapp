"""Meta-filter execution gate (Task 3, trade-profitability plan).

``meta_filter_block(db, settings, asset, direction, snapshot_dict) -> str``:
``""`` means take (or fail-open — never block on a broken input), otherwise
the return is a block reason containing ``meta_filter``. The function is
wired into ``execution._gate_blocked`` as one reject (visible in the
``order_blocked`` lifecycle log).

Artifact-gated by design: the newest
``backend/artifacts/meta-filter-*.json`` is loaded; the gate only judges
when that artifact ``holds_out == True`` AND ``trained_at`` is within 90
days AND every artifact feature derives from the snapshot. Anything else
fails OPEN (``""`` + a log line) — a missing/stale/collapsed model must
never halt trading.

These tests point ``execution.META_FILTER_ARTIFACTS_DIR`` at tmp artifacts
with zero weights + a fixed bias, so the probability is exactly
``sigmoid(bias)`` whatever the snapshot is — deterministic, never coupled
to the developer's last training run.

Run from backend/: py -3 -m pytest tests/test_meta_filter_gate.py -q
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.schemas import AppSettings
from app.services import execution
from tests.test_workers import FakeDatabase


FEATURES9 = ("adx", "rsi", "macd_hist", "atr_pct", "volatility_index",
             "chg20", "ema_gap_atr", "st_agree", "macd_agree")


def _snapshot(**over) -> dict:
    """Indicator snapshot (same shape as quotes.fetch_all_snapshots rows)."""
    base = {
        "asset": "EURUSD", "price": 1.1000,
        "ema_fast": 1.1050, "ema_slow": 1.0900,
        "adx": 30.0, "supertrend_dir": 1,
        "rsi": 55.0, "macd_hist": 0.1,
        "price_change_pct_20": 1.0,
        "atr_pct": 1.0, "volatility_index": 10.0,
        "news_sentiment": 0.0, "high_impact_event": False,
        "breakout_state": 0, "breakout_level": 0.0,
    }
    base.update(over)
    return base


def _settings(**over) -> AppSettings:
    base = dict(
        capital=10_000.0, min_confidence=70.0,
        kill_daily_loss_pct=2.0, kill_weekly_loss_pct=5.0,
        kill_monthly_loss_pct=8.0, max_drawdown_pct=10.0,
        max_trades_daily=6, max_trades_weekly=30, max_open_positions=4,
        risk_per_trade_pct=1.0, correlation_cap=80.0,
        news_block_minutes=30, order_mode="auto",
    )
    base.update(over)
    return AppSettings(**base)


def _write_artifact(directory, *, holds_out=True, threshold=0.65,
                    trained_at=None, bias=-2.0, name="meta-filter-test0000.json"):
    """Synthetic HOLDS OUT / COLLAPSED artifact with P == sigmoid(bias)."""
    if trained_at is None:
        trained_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    artifact = {
        "run_id": "test0000test0000test0000test0000",
        "trained_at": trained_at,
        "features": list(FEATURES9),
        "stats": {f: {"kept": True, "mean": 0.0, "std": 1.0}
                  for f in FEATURES9},
        "model": {"weights": [0.0] * len(FEATURES9), "bias": bias,
                  "iters": 400, "l2": 1.0, "lr": 1.0},
        "evaluation": {
            "train_n": 1000, "test_n": 400,
            "base_train_mean_r": 0.10, "base_test_mean_r": 0.05,
            "base_test_precision": 0.50, "thresholds": [],
            "picked": {"threshold": threshold, "train_n": 200,
                       "train_mean_r": 0.40, "test_n": 80,
                       "test_mean_r": 0.35, "test_precision": 0.65},
            "holds_out": holds_out,
        },
        "dropped_features": ["ema_agree"],
    }
    path = Path(str(directory)) / name
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def _point_at(monkeypatch, directory):
    monkeypatch.setattr(execution, "META_FILTER_ARTIFACTS_DIR",
                        Path(str(directory)))


def test_missing_artifact_never_blocks(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)  # empty dir: no artifact at all
    assert execution.meta_filter_block(
        FakeDatabase(), _settings(), "EURUSD", "BUY",
        _snapshot()) == ""


def test_stale_artifact_never_blocks(monkeypatch, tmp_path):
    # trained_at older than 90 days: a would-block model still passes open.
    _point_at(monkeypatch, tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    _write_artifact(tmp_path, trained_at=old, holds_out=True, bias=-2.0)
    assert execution.meta_filter_block(
        FakeDatabase(), _settings(), "EURUSD", "BUY",
        _snapshot()) == ""


def test_low_proba_blocks_with_reason(monkeypatch, tmp_path):
    # sigmoid(-2.0) ~= 0.12 < 0.65 threshold -> block, reason names the gate.
    _point_at(monkeypatch, tmp_path)
    _write_artifact(tmp_path, holds_out=True, threshold=0.65, bias=-2.0)
    reason = execution.meta_filter_block(
        FakeDatabase(), _settings(), "EURUSD", "BUY", _snapshot())
    assert "meta_filter" in reason


def test_high_proba_passes(monkeypatch, tmp_path):
    # sigmoid(+2.0) ~= 0.88 >= 0.65 threshold -> take.
    _point_at(monkeypatch, tmp_path)
    _write_artifact(tmp_path, holds_out=True, threshold=0.65, bias=+2.0)
    assert execution.meta_filter_block(
        FakeDatabase(), _settings(), "EURUSD", "BUY",
        _snapshot()) == ""


def test_collapsed_artifact_never_blocks(monkeypatch, tmp_path):
    # holds_out False: a would-block model still passes open.
    _point_at(monkeypatch, tmp_path)
    _write_artifact(tmp_path, holds_out=False, threshold=0.65, bias=-2.0)
    assert execution.meta_filter_block(
        FakeDatabase(), _settings(), "EURUSD", "BUY",
        _snapshot()) == ""


def test_meta_threshold_override_half(monkeypatch, tmp_path):
    # sigmoid(0.28) ~= 0.57 < artifact 0.65 -> would block, but the settings
    # override (0.5) takes instead -> take ("").
    _point_at(monkeypatch, tmp_path)
    _write_artifact(tmp_path, holds_out=True, threshold=0.65, bias=0.28)
    assert execution.meta_filter_block(
        FakeDatabase(), _settings(meta_filter_threshold=0.5),
        "EURUSD", "BUY", _snapshot()) == ""
    assert AppSettings().meta_filter_threshold == 0.5


def test_meta_threshold_zero_uses_artifact(monkeypatch, tmp_path):
    # Explicit 0 = "use artifact picked threshold": sigmoid(0.28) ~= 0.57
    # < artifact 0.65 -> must BLOCK (no 0.5 override applied).
    _point_at(monkeypatch, tmp_path)
    _write_artifact(tmp_path, holds_out=True, threshold=0.65, bias=0.28)
    reason = execution.meta_filter_block(
        FakeDatabase(), _settings(meta_filter_threshold=0),
        "EURUSD", "BUY", _snapshot())
    assert "meta_filter" in reason
    assert "take 0.65" in reason


def test_gate_blocked_reports_meta_reject(monkeypatch, tmp_path):
    """The _gate_blocked seam surfaces the filter as one reject."""
    _point_at(monkeypatch, tmp_path)
    _write_artifact(tmp_path, holds_out=True, threshold=0.65, bias=-2.0)
    db = FakeDatabase()
    report = execution._gate_blocked(
        db, _settings(), "demo", "EURUSD", 85.0, 80.0,
        entry=1.0850, stop_loss=1.0800, direction="BUY",
        meta_snapshot=_snapshot())
    assert not report.allowed
    assert any("meta_filter" in r for r in report.rejects)


class _NoOrderBroker:
    """Broker fake that records placements (none expected here)."""

    def __init__(self):
        self.orders = []

    async def place_order(self, order):
        self.orders.append(order)
        raise AssertionError("meta-filtered order must never reach the broker")


@pytest.mark.asyncio
async def test_execute_signal_logs_meta_order_blocked(monkeypatch, tmp_path):
    """End to end: a skipped setup never reaches the broker and the skip is
    visible as an order_blocked lifecycle row."""
    _point_at(monkeypatch, tmp_path)
    _write_artifact(tmp_path, holds_out=True, threshold=0.65, bias=-2.0)

    async def fake_trusted(assets, **_kw):
        return {}, {}

    async def fake_snaps(assets, **_kw):
        return {a: _snapshot() for a in (assets or [])}

    monkeypatch.setattr(execution.quotes, "fetch_trusted_spot", fake_trusted)
    monkeypatch.setattr(execution.quotes, "fetch_all_snapshots", fake_snaps)

    db = FakeDatabase()
    broker = _NoOrderBroker()
    report = await execution.execute_signal(
        db, broker, None, _settings(),
        user_id="demo", asset="EURUSD", direction="BUY",
        entry=1.0850, stop_loss=1.0800, take_profit=1.0950,
        confidence=85.0, opportunity=80.0, signal_id="sig-meta",
        source="auto", signal_row={"id": "sig-meta", "asset": "EURUSD",
                                   "direction": "BUY"})
    assert not report.allowed
    assert any("meta_filter" in r for r in report.rejects)
    assert broker.orders == []
    blocked = [row for table, row in db.inserted
               if table == "signal_logs" and row.get("event") == "order_blocked"]
    assert blocked, "expected an order_blocked lifecycle row"
    assert any("meta_filter" in str(row.get("reason") or "")
               for row in blocked)
