"""Re-entry cooldown tests — Gate 2b (execution) + shared helper.

Stops the 1-minute close→reopen loop: guard closes on SL/TP/time-stop and the
auto-trader would otherwise re-fire the still-pending signal next cycle
(30-min TTL). The gate measures from paper_trades.closed_at per asset.

Run from backend/: python -m pytest tests/test_reentry_cooldown.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.schemas import AppSettings
from app.services import execution
from tests.test_workers import FakeDatabase


class FakeBroker:
    def __init__(self):
        self.orders: list = []
        self._next = 1

    async def place_order(self, order):
        self.orders.append(order)
        ticket = f"PAPER-{self._next:06d}"
        self._next += 1
        return SimpleNamespace(ok=True, broker_order_id=ticket, message="opened")

    async def all_positions(self):
        return []


class FakeNotifier:
    def __init__(self):
        self.sent: list = []

    async def notify(self, user_id, ntype, message):
        self.sent.append((ntype, message))


def clean_settings(**over) -> AppSettings:
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


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture(autouse=True)
def _no_network_spot(monkeypatch):
    async def fake_spot(assets, **_kw):
        return {}, {}
    monkeypatch.setattr(execution.quotes, "fetch_spot_prices", fake_spot)


def _closed_row(asset: str, minutes_ago: float) -> dict:
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "id": "closed-1", "asset": asset, "direction": "BUY",
        "volume": 0.01, "status": "closed", "pnl": 1.0,
        "closed_at": ts, "created_at": ts,
    }


@pytest.mark.asyncio
async def test_cooldown_blocks_within_window(broker, notifier):
    """AUDNZD closed 1 min ago + cooldown 30 → blocked with Thai reason."""
    db = FakeDatabase(rows={"paper_trades": [_closed_row("AUDNZD", 1.0)]})
    s = clean_settings(reentry_cooldown_min=30)
    report = await execution.execute_signal(
        db, broker, notifier, s,
        user_id="demo", asset="AUDNZD", direction="BUY",
        entry=1.10000, stop_loss=1.09324, take_profit=1.11352,
        confidence=85.0, opportunity=80.0, signal_id="sig-cool-1", source="auto",
    )
    assert not report.allowed
    assert any("cooldown" in c for c in report.checks)
    assert any("เพิ่งปิด AUDNZD" in r and "cooldown" in r for r in report.rejects)
    assert broker.orders == []
    blocked = [row for table, row in db.inserted
               if table == "signal_logs" and row.get("event") == "order_blocked"]
    assert blocked and "เพิ่งปิด AUDNZD" in blocked[0]["reason"]


@pytest.mark.asyncio
async def test_cooldown_expired_allows(broker, notifier):
    """Closed 40 min ago + cooldown 30 → allowed."""
    db = FakeDatabase(rows={"paper_trades": [_closed_row("AUDNZD", 40.0)]})
    s = clean_settings(reentry_cooldown_min=30)
    report = await execution.execute_signal(
        db, broker, notifier, s,
        user_id="demo", asset="AUDNZD", direction="BUY",
        entry=1.10000, stop_loss=1.09324, take_profit=1.11352,
        confidence=85.0, opportunity=80.0, signal_id="sig-cool-2", source="auto",
    )
    assert report.allowed, report.rejects
    assert len(broker.orders) == 1


@pytest.mark.asyncio
async def test_cooldown_different_asset_unaffected(broker, notifier):
    """AUDNZD closed 1 min ago must NOT block EURUSD."""
    db = FakeDatabase(rows={"paper_trades": [_closed_row("AUDNZD", 1.0)]})
    s = clean_settings(reentry_cooldown_min=30)
    report = await execution.execute_signal(
        db, broker, notifier, s,
        user_id="demo", asset="EURUSD", direction="BUY",
        entry=1.0850, stop_loss=1.0800, take_profit=1.0950,
        confidence=85.0, opportunity=80.0, signal_id="sig-cool-3", source="auto",
    )
    assert report.allowed, report.rejects
    assert len(broker.orders) == 1


@pytest.mark.asyncio
async def test_cooldown_zero_disables(broker, notifier):
    """cooldown 0 → closed 1 min ago still opens (feature off)."""
    db = FakeDatabase(rows={"paper_trades": [_closed_row("AUDNZD", 1.0)]})
    s = clean_settings(reentry_cooldown_min=0)
    report = await execution.execute_signal(
        db, broker, notifier, s,
        user_id="demo", asset="AUDNZD", direction="BUY",
        entry=1.10000, stop_loss=1.09324, take_profit=1.11352,
        confidence=85.0, opportunity=80.0, signal_id="sig-cool-4", source="auto",
    )
    assert report.allowed, report.rejects
    assert len(broker.orders) == 1


def test_cooldown_helper_direct_unit():
    """Helper math: elapsed/left, case-insensitive, missing closed_at."""
    now = datetime.now(timezone.utc)
    db = FakeDatabase(rows={"paper_trades": [
        {**_closed_row("audnzd", 5.0)},  # lowercase asset matches AUDNZD
        {"id": "bad", "asset": "AUDNZD", "status": "closed"},  # no closed_at → skip
    ]})
    s = clean_settings(reentry_cooldown_min=30)
    msg = execution.reentry_cooldown_block(db, s, "AUDNZD", now=now)
    assert "เพิ่งปิด AUDNZD" in msg
    assert "5.0 นาที" in msg and "เหลืออีก 25.0 นาที" in msg
    # expired → ""
    old = FakeDatabase(rows={"paper_trades": [_closed_row("AUDNZD", 31.0)]})
    assert execution.reentry_cooldown_block(old, s, "AUDNZD", now=now) == ""


def test_cooldown_db_error_fail_open():
    """Broken history read → "" so one bad read never halts all trading."""

    class BrokenDb(FakeDatabase):
        def select(self, *a, **k):
            raise RuntimeError("db down")

    s = clean_settings(reentry_cooldown_min=30)
    assert execution.reentry_cooldown_block(BrokenDb(), s, "AUDNZD") == ""


def test_cooldown_defaults_and_presets():
    """Default 30; conservative 60 / moderate 30 / aggressive 15."""
    from app.models.schemas import RISK_PRESETS, RiskProfile
    assert AppSettings().reentry_cooldown_min == 30
    assert RISK_PRESETS[RiskProfile.conservative]["reentry_cooldown_min"] == 60
    assert RISK_PRESETS[RiskProfile.moderate]["reentry_cooldown_min"] == 30
    assert RISK_PRESETS[RiskProfile.aggressive]["reentry_cooldown_min"] == 15
