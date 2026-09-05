"""Auto-trader Phase 1 tests — gate pipeline, sizing, pause switch, workers.

Covers the three Phase-1 fixes:
  1. Safety gate: /approve (and the auto trader) run pause → kill switch →
     frequency → news → correlation → risk officer BEFORE place_order, and
     volume comes from risk_to_lot(settings), not the old hardcoded 0.01.
  2. SL/TP enforcement: position_guard closes paper positions on SL/TP touch
     and journals the close into paper_trades.
  3. Real kill switch: /pause, LINE /pause and the gate share one state.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_auto_trader.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.schemas import AppSettings
from app.services import execution
from app.workers import auto_trader, position_guard
from tests.test_workers import FakeDatabase


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeBroker:
    """Records orders; returns configurable results."""

    def __init__(self):
        self.orders: list = []
        self._next = 1

    async def connect(self):
        return True

    async def place_order(self, order):
        self.orders.append(order)
        ticket = f"PAPER-{self._next:06d}"
        self._next += 1
        return SimpleNamespace(ok=True, broker_order_id=ticket, message="opened")

    async def close_position(self, ticket):
        return SimpleNamespace(ok=True, broker_order_id=ticket, message="closed")

    async def positions(self, user_id):
        return []

    async def all_positions(self):
        return []

    async def quote(self, asset):
        return 0.0

    async def mark_price(self, ticket):
        return 0.0


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def notify(self, user_id, ntype, message):
        self.sent.append((ntype, message))


class SimpleResult:
    def __init__(self, data):
        self.data = data


class FakeKVQuery:
    """Chainable supabase-style query limited to what execution.py uses."""

    def __init__(self, store: dict, table: str):
        self._store, self._table, self._id = store, table, None

    def select(self, *_a):
        return self

    def eq(self, _col, val):
        self._id = val
        return self

    def limit(self, _n):
        return self

    def upsert(self, row):
        self._store.setdefault(self._table, {})[row.get("id")] = dict(row)
        return self

    def execute(self):
        rows = self._store.get(self._table, {})
        data = [rows[self._id]] if self._id is not None and self._id in rows else []
        return SimpleResult(data)


class FakeKVClient:
    """Key-value supabase stand-in for trading_pause + trading_settings rows."""

    def __init__(self, tables: dict[str, dict] | None = None):
        self.store: dict[str, dict] = tables or {}

    def table(self, name: str) -> FakeKVQuery:
        return FakeKVQuery(self.store, name)


def db_with_client(tables: dict[str, list] | None = None) -> FakeDatabase:
    """FakeDatabase + _client so pause/settings round-trips behave like prod.

    ``tables`` seeds both db.rows (dict-table queries) and the fake supabase
    client (settings/pause KV reads)."""
    db = FakeDatabase(rows={k: list(v) for k, v in (tables or {}).items()})
    db._client = FakeKVClient()
    return db


def clean_settings(**over) -> AppSettings:
    """Settings that pass every gate by default."""
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


# ---------------------------------------------------------------------------
# 1. Gate pipeline + position sizing
# ---------------------------------------------------------------------------
class TestGatePipeline:
    @pytest.mark.asyncio
    async def test_clean_signal_fires_and_journals(self, broker, notifier):
        db = FakeDatabase()
        s = clean_settings()
        # EURUSD, SL 50 pips → risk_to_lot: $100 risk / (0.0050 × 100k) = 0.20 lots
        report = await execution.execute_signal(
            db, broker, notifier, s,
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.0850, stop_loss=1.0800, take_profit=1.0950,
            confidence=85.0, opportunity=80.0, signal_id="sig-1", source="auto",
        )
        assert report.allowed, report.rejects
        assert len(broker.orders) == 1
        assert broker.orders[0].volume == pytest.approx(0.2, abs=0.02)
        journal = db.rows.get("paper_trades", [])
        assert len(journal) == 1
        assert journal[0]["status"] == "open"
        assert journal[0]["source"] == "auto"
        assert journal[0]["ticket"] == "PAPER-000001"

    @pytest.mark.asyncio
    async def test_gold_confidence_override_reaches_risk_officer(self, broker, notifier):
        """Regression: XAUUSD 65.8% passed the scanner (Min Confidence (gold)
        = 60) but Gate 5's RiskOfficer vetoed it with a hardcoded 'confidence
        65 < 70'. The officer must use the same effective threshold."""
        db = FakeDatabase()
        s = clean_settings(min_confidence_gold=60.0)
        report = await execution.execute_signal(
            db, broker, notifier, s,
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2390.0, take_profit=2420.0,
            confidence=65.8, opportunity=65.0, signal_id="sig-gold", source="auto",
        )
        assert report.allowed, report.rejects
        assert len(broker.orders) == 1
        # and the same confidence on the base bar still blocks
        db2 = FakeDatabase()
        s2 = clean_settings()  # min_confidence=70, no gold override
        report2 = await execution.execute_signal(
            db2, broker, notifier, s2,
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2390.0, take_profit=2420.0,
            confidence=65.8, opportunity=65.0, signal_id="sig-gold2", source="auto",
        )
        assert not report2.allowed
        assert any("confidence" in r for r in report2.rejects)

    @pytest.mark.asyncio
    async def test_gold_min_lot_override_reaches_order(self, broker, notifier):
        """min_lot_gold (ขนาด Lot ขั้นต่ำ gold) floors the REAL XAUUSD order
        volume; an FX order in the same settings keeps the base min_lot."""
        db = FakeDatabase()
        s = clean_settings(capital=100.0, risk_per_trade_pct=0.1,
                           min_lot=0.01, min_lot_gold=0.05)
        report = await execution.execute_signal(
            db, broker, notifier, s,
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2350.0, take_profit=2450.0,
            confidence=85.0, opportunity=80.0, signal_id="sig-goldlot", source="auto",
        )
        assert report.allowed, report.rejects
        assert len(broker.orders) == 1
        # risk_to_lot gives ~0.0002 lots → floored to the gold override 0.05
        assert broker.orders[0].volume == pytest.approx(0.05, abs=1e-9)
        # FX order under the same settings keeps the base floor 0.01
        db2 = FakeDatabase()
        report2 = await execution.execute_signal(
            db2, broker, notifier, s,
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.0850, stop_loss=1.0800, take_profit=1.0950,
            confidence=85.0, opportunity=80.0, signal_id="sig-fxlot", source="auto",
        )
        assert report2.allowed, report2.rejects
        assert broker.orders[1].volume == pytest.approx(0.01, abs=1e-9)

    @pytest.mark.asyncio
    async def test_sl_distance_mode_short_widens_tightens_sl(self, broker, notifier):
        """sl_distance_mode=สั้น (short, ×1.0 ATR): SL re-derived from the
        stored กลาง (×1.5) row — distance ×(1.0/1.5), TP keeps the row's RR.
        Sizing follows the re-derived (tighter) SL → more lots."""
        db = FakeDatabase()
        s = clean_settings(sl_distance_mode="short")
        report = await execution.execute_signal(
            db, broker, notifier, s,
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.0850, stop_loss=1.0800, take_profit=1.0950,  # กลาง: 50 pips, RR 2
            confidence=85.0, opportunity=80.0, signal_id="sig-sl", source="auto",
        )
        assert report.allowed, report.rejects
        order = broker.orders[0]
        assert order.stop_loss == pytest.approx(round(1.0850 - 0.0050 * (1.0 / 1.5), 5), abs=1e-9)  # 33.3 pips
        assert order.take_profit == pytest.approx(round(1.0850 + 0.0033 * 2, 5), abs=1e-4)          # RR kept
        # sizing uses the re-derived SL: $100 / (0.00333 × 100k) ≈ 0.30 lots
        assert order.volume == pytest.approx(0.3, abs=0.03)

    @pytest.mark.asyncio
    async def test_sl_distance_mode_long_widens_sl(self, broker, notifier):
        """sl_distance_mode=ยาว (long, ×2.0 ATR): distance ×(2.0/1.5)."""
        db = FakeDatabase()
        s = clean_settings(sl_distance_mode="long")
        report = await execution.execute_signal(
            db, broker, notifier, s,
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.0850, stop_loss=1.0800, take_profit=1.0950,
            confidence=85.0, opportunity=80.0, signal_id="sig-ll", source="auto",
        )
        assert report.allowed, report.rejects
        order = broker.orders[0]
        assert order.stop_loss == pytest.approx(round(1.0850 - 0.0050 * (2.0 / 1.5), 5), abs=1e-9)  # 66.7 pips

    @pytest.mark.asyncio
    async def test_sl_distance_mode_medium_keeps_stored_prices(self, broker, notifier):
        """Default (กลาง) → stored row prices pass through untouched."""
        db = FakeDatabase()
        s = clean_settings(sl_distance_mode="medium")
        report = await execution.execute_signal(
            db, broker, notifier, s,
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.0850, stop_loss=1.0800, take_profit=1.0950,
            confidence=85.0, opportunity=80.0, signal_id="sig-md", source="auto",
        )
        assert report.allowed, report.rejects
        order = broker.orders[0]
        assert order.stop_loss == 1.0800
        assert order.take_profit == 1.0950

    @pytest.mark.asyncio
    async def test_pause_blocks_execution(self, broker, notifier):
        db = db_with_client()
        execution.set_pause(db, True, "testing")
        report = await execution.execute_signal(
            db, broker, notifier, clean_settings(),
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2350.0, take_profit=2500.0,
            confidence=90.0, opportunity=90.0, signal_id="sig-2", source="auto",
        )
        assert not report.allowed
        assert any("paused" in r.lower() for r in report.rejects)
        assert broker.orders == []
        assert db.rows.get("paper_trades", []) == []

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_after_losses(self, broker, notifier):
        db = FakeDatabase()
        now = datetime.now(timezone.utc).isoformat()
        # journal shows a big daily loss → kill switch must engage
        db.rows["paper_trades"] = [{
            "asset": "XAUUSD", "direction": "SELL", "volume": 0.5,
            "status": "closed", "pnl": -400.0,  # 4% of 10k > 2% daily limit
            "closed_at": now, "created_at": now,
        }]
        report = await execution.execute_signal(
            db, broker, notifier, clean_settings(),
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2350.0, take_profit=2500.0,
            confidence=90.0, opportunity=90.0, signal_id="sig-3", source="auto",
        )
        assert not report.allowed
        assert any("kill switch" in r.lower() for r in report.rejects)
        assert broker.orders == []

    @pytest.mark.asyncio
    async def test_daily_trade_limit_blocks(self, broker, notifier):
        db = FakeDatabase()
        now = datetime.now(timezone.utc).isoformat()
        db.rows["paper_trades"] = [
            {"asset": "EURUSD", "direction": "BUY", "volume": 0.01,
             "status": "open", "pnl": 0, "created_at": now}
            for _ in range(6)
        ]
        report = await execution.execute_signal(
            db, broker, notifier, clean_settings(max_trades_daily=6),
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2350.0, take_profit=2500.0,
            confidence=90.0, opportunity=90.0, signal_id="sig-4", source="auto",
        )
        assert not report.allowed
        assert any("daily limit" in r.lower() for r in report.rejects)

    @pytest.mark.asyncio
    async def test_low_confidence_rejected(self, broker, notifier):
        db = FakeDatabase()
        report = await execution.execute_signal(
            db, broker, notifier, clean_settings(min_confidence=70),
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2350.0, take_profit=2500.0,
            confidence=55.0, opportunity=90.0, signal_id="sig-5", source="auto",
        )
        assert not report.allowed
        assert any("confidence" in r.lower() for r in report.rejects)

    @pytest.mark.asyncio
    async def test_gold_min_confidence_blocks_gold_order(self, broker, notifier):
        """Min Confidence (gold) applies to ORDER OPENING: confidence 75 passes
        the base gate (70) but must be blocked by the gold override (90)."""
        db = FakeDatabase()
        report = await execution.execute_signal(
            db, broker, notifier,
            clean_settings(min_confidence=70.0, min_confidence_gold=90.0),
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2350.0, take_profit=2500.0,
            confidence=75.0, opportunity=90.0, signal_id="sig-gold-1",
            source="auto",
        )
        assert not report.allowed
        assert any("confidence" in r.lower() for r in report.rejects)
        assert broker.orders == []
        assert db.rows.get("paper_trades", []) == []

    @pytest.mark.asyncio
    async def test_gold_min_confidence_does_not_block_fx_order(self, broker, notifier):
        """The gold override must NOT leak into other assets — the same
        confidence 75 on EURUSD still passes the base gate (70)."""
        db = FakeDatabase()
        report = await execution.execute_signal(
            db, broker, notifier,
            clean_settings(min_confidence=70.0, min_confidence_gold=90.0),
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.0850, stop_loss=1.0800, take_profit=1.0950,
            confidence=75.0, opportunity=90.0, signal_id="sig-gold-2",
            source="auto",
        )
        assert report.allowed, report.rejects
        assert len(broker.orders) == 1

    @pytest.mark.asyncio
    async def test_gold_min_confidence_unset_uses_base(self, broker, notifier):
        """No gold override → gold orders gate at the base threshold."""
        db = FakeDatabase()
        report = await execution.execute_signal(
            db, broker, notifier, clean_settings(min_confidence=70.0),
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2350.0, take_profit=2500.0,
            confidence=75.0, opportunity=90.0, signal_id="sig-gold-3",
            source="auto",
        )
        assert report.allowed, report.rejects
        assert len(broker.orders) == 1

    @pytest.mark.asyncio
    async def test_news_danger_blocks(self, broker, notifier):
        db = FakeDatabase()
        soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        db.rows["economic_calendar"] = [
            {"event": "FOMC", "currency": "USD", "event_time": soon,
             "impact": "high"}]
        report = await execution.execute_signal(
            db, broker, notifier, clean_settings(),
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=2400.0, stop_loss=2350.0, take_profit=2500.0,
            confidence=90.0, opportunity=90.0, signal_id="sig-6", source="auto",
        )
        assert not report.allowed
        assert any("news" in r.lower() for r in report.rejects)

    def test_size_position_uses_risk_to_lot(self):
        s = clean_settings(capital=10_000.0, risk_per_trade_pct=1.0)
        # $100 risk / (0.0050 × 100k) = 0.20 lots (EURUSD, 50-pip stop)
        assert execution.size_position(s, 1.0850, 1.0800) == pytest.approx(0.2, abs=0.02)

    def test_size_position_gold_stop_gives_reasonable_lots(self):
        s = clean_settings(capital=10_000.0, risk_per_trade_pct=1.0)
        # XAUUSD SL 50 pts against the 100k FX contract: 0.02 lots, min-floor applies
        lots = execution.size_position(s, 2400.0, 2350.0)
        assert 0.01 <= lots <= 0.03

    def test_size_position_floor_at_min_lot(self):
        s = clean_settings(capital=100.0, risk_per_trade_pct=0.1)
        assert execution.size_position(s, 1.0850, 1.0800) >= 0.01

    def test_size_position_honors_min_lot_setting(self):
        """min_lot from Settings replaces the old hardcoded 0.01 floor."""
        s = clean_settings(capital=100.0, risk_per_trade_pct=0.1, min_lot=0.02)
        # risk_to_lot gives ~0.0002 lots → floored to the user's 0.02
        assert execution.size_position(s, 1.0850, 1.0800) == pytest.approx(0.02, abs=1e-9)

    def test_size_position_min_lot_does_not_shrink_risk_lots(self):
        """min_lot is a FLOOR — a large risk-based size is never reduced."""
        s = clean_settings(capital=10_000.0, risk_per_trade_pct=1.0, min_lot=0.02)
        # $100 risk / (0.0050 × 100k) = 0.20 lots > 0.02 → sizing unchanged
        assert execution.size_position(s, 1.0850, 1.0800) == pytest.approx(0.2, abs=0.02)

    def test_size_position_gold_override_applies_to_gold_only(self):
        """min_lot_gold raises the floor for XAUUSD; FX keeps the base min_lot."""
        s = clean_settings(capital=100.0, risk_per_trade_pct=0.1,
                           min_lot=0.01, min_lot_gold=0.05)
        # risk_to_lot gives ~0.0002 lots → gold floored to 0.05, FX to 0.01
        assert execution.size_position(s, 2400.0, 2350.0, asset="XAUUSD") == pytest.approx(0.05, abs=1e-9)
        assert execution.size_position(s, 1.0850, 1.0800, asset="EURUSD") == pytest.approx(0.01, abs=1e-9)

    def test_size_position_gold_override_unset_uses_base(self):
        """No min_lot_gold → gold uses the base min_lot (backwards compatible)."""
        s = clean_settings(capital=100.0, risk_per_trade_pct=0.1, min_lot=0.02)
        assert execution.size_position(s, 2400.0, 2350.0, asset="XAUUSD") == pytest.approx(0.02, abs=1e-9)

    def test_size_position_gold_override_does_not_shrink_risk_lots(self):
        """min_lot_gold is a FLOOR — a large risk-based gold size is never reduced."""
        s = clean_settings(capital=10_000.0, risk_per_trade_pct=1.0,
                           min_lot=0.01, min_lot_gold=0.05)
        # XAUUSD SL 50 pts against the 100k FX contract ≈ 0.02 lots < 0.05 → floored
        assert execution.size_position(s, 2400.0, 2350.0, asset="XAUUSD") == pytest.approx(0.05, abs=1e-9)
        # huge risk-based size stays untouched: $400k risk / (50 × 100k) = 0.08 lots
        s2 = clean_settings(capital=20_000_000.0, risk_per_trade_pct=2.0,
                            min_lot=0.01, min_lot_gold=0.05)
        assert execution.size_position(s2, 2400.0, 2350.0, asset="XAUUSD") == pytest.approx(0.08, abs=1e-9)


# ---------------------------------------------------------------------------
# 2. AutoTrader worker
# ---------------------------------------------------------------------------
class TestAutoTrader:
    @pytest.mark.asyncio
    async def test_semi_auto_mode_does_nothing(self, broker, notifier):
        db = db_with_client()
        db._client.store["trading_settings"] = {1: {"id": 1, "order_mode": "semi_auto"}}
        db.rows["signals"] = [
            {"id": "s1", "asset": "XAUUSD", "direction": "buy", "confidence": 90.0,
             "entry": 2400.0, "stop_loss": 2350.0, "take_profit": 2500.0,
             "approval": "pending"}]
        out = await auto_trader.trade_once(db, broker, notifier)
        assert out["picked"] == 0 and out["fired"] == 0
        assert broker.orders == []

    @pytest.mark.asyncio
    async def test_auto_mode_fires_pending_signal(self, broker, notifier):
        db = FakeDatabase(rows={"signals": [
            {"id": "s1", "asset": "XAUUSD", "direction": "buy", "confidence": 90.0,
             "entry": 2400.0, "stop_loss": 2350.0, "take_profit": 2500.0,
             "approval": "pending",
             "created_at": datetime.now(timezone.utc).isoformat()}]})
        out = await auto_trader.trade_once(db, broker, notifier)
        assert out["fired"] == 1
        assert len(broker.orders) == 1
        assert db.rows["signals"][0]["approval"] == "approved"
        assert len(db.rows.get("paper_trades", [])) == 1

    @pytest.mark.asyncio
    async def test_blocked_signal_stays_pending(self, broker, notifier):
        db = FakeDatabase(rows={"signals": [
            {"id": "s1", "asset": "XAUUSD", "direction": "buy", "confidence": 50.0,
             "entry": 2400.0, "stop_loss": 2350.0, "take_profit": 2500.0,
             "approval": "pending",
             "created_at": datetime.now(timezone.utc).isoformat()}]})
        out = await auto_trader.trade_once(db, broker, notifier)
        assert out["fired"] == 0 and out["blocked"] == 1
        assert broker.orders == []
        assert db.rows["signals"][0]["approval"] == "pending"

    @pytest.mark.asyncio
    async def test_open_position_blocks_duplicate_fire(self, broker, notifier):
        """Regression (2026-09-04): the auto-trader fired every pending signal
        with no check for an existing open position on the same asset — the
        account stacked 14 positions (EURUSD×5, AUDUSD×5, XAUUSD×4) in ~10
        minutes. A pending signal for an asset with an open position must be
        skipped, not fired, and must stay pending."""
        db = FakeDatabase(rows={
            "signals": [
                {"id": "s1", "asset": "XAUUSD", "direction": "buy",
                 "confidence": 90.0, "entry": 2400.0, "stop_loss": 2350.0,
                 "take_profit": 2500.0, "approval": "pending",
                 "created_at": datetime.now(timezone.utc).isoformat()}],
            "paper_trades": [
                {"id": "p1", "asset": "XAUUSD", "status": "open"}]})
        out = await auto_trader.trade_once(db, broker, notifier)
        assert out["fired"] == 0 and out["skipped"] == 1
        assert broker.orders == []
        assert db.rows["signals"][0]["approval"] == "pending"

    @pytest.mark.asyncio
    async def test_stale_signal_skipped(self, broker, notifier):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        db = FakeDatabase(rows={"signals": [
            {"id": "s1", "asset": "XAUUSD", "direction": "buy", "confidence": 90.0,
             "entry": 2400.0, "stop_loss": 2350.0, "take_profit": 2500.0,
             "approval": "pending", "created_at": old}]})
        out = await auto_trader.trade_once(db, broker, notifier)
        assert out["fired"] == 0
        assert out["expired"] == 1
        assert broker.orders == []
        # stale rows leave the pending queue — marked expired (009 migration)
        assert db.rows["signals"][0]["approval"] == "expired"


# ---------------------------------------------------------------------------
# 3. Position guard (SL/TP enforcement)
# ---------------------------------------------------------------------------
def make_pos(direction="BUY", entry=100.0, sl=95.0, tp=110.0, price=100.0,
             asset="XAUUSD"):
    return SimpleNamespace(
        ticket="PAPER-000001", user_id="demo", asset=asset,
        direction=direction, volume=1.0, entry_price=entry,
        stop_loss=sl, take_profit=tp, current_price=price)


@pytest.fixture(autouse=True)
def _fake_live_marks(monkeypatch):
    """Pin the live spot feed so guard tests never touch the network.

    The guard now prefers live marks (that's the TP-not-closing fix); tests
    inject exactly the price the scenario needs via this registry.
    """
    registry: dict[str, float] = {}

    async def fake_fetch(assets):
        return ({a: registry[a] for a in assets if a in registry}, {})

    monkeypatch.setattr(position_guard.quotes, "fetch_spot_prices", fake_fetch)
    return registry


class GuardBroker(FakeBroker):
    """Fake broker carrying open positions with injected prices."""

    def __init__(self, positions):
        super().__init__()
        self._positions = {p.ticket: p for p in positions}  # the book
        self._pos = self._positions  # same dict (alias)
        self._seq = 0
        self.closed: list[str] = []

    async def all_positions(self):
        return list(self._pos.values())

    async def mark_price(self, ticket):
        return self._pos[ticket].current_price

    async def close_position(self, ticket):
        self.closed.append(ticket)
        return SimpleNamespace(ok=True, broker_order_id=ticket, message="closed")


class TestPositionGuard:
    @pytest.mark.asyncio
    async def test_stop_loss_closes_buy(self, notifier, _fake_live_marks):
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "pt1", "ticket": "PAPER-000001", "asset": "XAUUSD",
             "direction": "BUY", "volume": 1.0, "entry_price": 100.0,
             "status": "open", "source": "auto"}]})
        broker = GuardBroker([make_pos(direction="BUY", price=94.0)])
        _fake_live_marks["XAUUSD"] = 94.0  # below SL 95 → SL hit
        out = await position_guard.guard_once(db, broker, notifier)
        assert out["closed"] == 1
        assert broker.closed == ["PAPER-000001"]
        row = db.rows["paper_trades"][0]
        assert row["status"] == "closed" and row["close_reason"] == "sl"
        assert row["pnl"] == pytest.approx(-600.0, abs=0.01)  # 1 lot XAUUSD = 100 oz
        assert notifier.sent and notifier.sent[0][0] == "stop_loss"

    @pytest.mark.asyncio
    async def test_take_profit_closes_sell(self, notifier, _fake_live_marks):
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "pt1", "ticket": "PAPER-000001", "asset": "XAUUSD",
             "direction": "SELL", "volume": 1.0, "entry_price": 100.0,
             "status": "open", "source": "auto"}]})
        broker = GuardBroker([make_pos(direction="SELL", entry=100.0,
                                       sl=105.0, tp=90.0, price=89.0)])
        _fake_live_marks["XAUUSD"] = 89.0  # below TP 90 on a SELL → TP hit
        out = await position_guard.guard_once(db, broker, notifier)
        assert out["closed"] == 1
        row = db.rows["paper_trades"][0]
        assert row["close_reason"] == "tp"
        assert row["pnl"] == pytest.approx(1100.0, abs=0.01)  # 1 lot XAUUSD = 100 oz
        assert notifier.sent[0][0] == "trade_closed"

    @pytest.mark.asyncio
    async def test_price_between_sl_tp_leaves_position_open(self, notifier, _fake_live_marks):
        db = FakeDatabase()
        broker = GuardBroker([make_pos(price=100.0)])
        _fake_live_marks["XAUUSD"] = 100.0  # between SL 95 and TP 110
        out = await position_guard.guard_once(db, broker, notifier)
        assert out["closed"] == 0
        assert broker.closed == []

    @pytest.mark.asyncio
    async def test_no_sl_tp_never_closes(self, notifier, _fake_live_marks):
        db = FakeDatabase()
        broker = GuardBroker([make_pos(sl=None, tp=None, price=50.0)])
        _fake_live_marks["XAUUSD"] = 50.0
        out = await position_guard.guard_once(db, broker, notifier)
        assert out["closed"] == 0

    @pytest.mark.asyncio
    async def test_stale_mark_but_live_tp_breach_closes(self, notifier, _fake_live_marks):
        """Regression: GBPUSD TP was breached live but the paper book never
        ticks, so mark_price() returned the entry forever and the position
        sat open. The guard must prefer the live mark over the stale one."""
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "pt1", "ticket": "PAPER-000001", "asset": "GBPUSD",
             "direction": "BUY", "volume": 0.01, "entry_price": 1.26797,
             "status": "open", "source": "auto"}]})
        # current_price pinned at entry (1.26797) — below TP 1.31286
        broker = GuardBroker([make_pos(asset="GBPUSD", entry=1.26797, sl=1.24553,
                                       tp=1.31286, price=1.26797)])
        _fake_live_marks["GBPUSD"] = 1.3536  # live price past TP
        out = await position_guard.guard_once(db, broker, notifier)
        assert out["closed"] == 1
        row = db.rows["paper_trades"][0]
        assert row["close_reason"] == "tp"

    @pytest.mark.asyncio
    async def test_rehydrate_restores_guard_enforcement_after_restart(self, notifier,
                                                                     _fake_live_marks):
        """Regression: the PaperBroker book is in-memory, so a Render restart
        wiped it — the DB still said "open" (visible on the monitor) but
        guard_once saw an empty book and stopped enforcing SL/TP forever.
        rehydrate_book() must rebuild the book from DB rows on startup."""
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "pt1", "ticket": "PAPER-000001", "user_id": "demo",
             "asset": "GBPUSD", "direction": "BUY", "volume": 0.01,
             "entry_price": 1.26797, "stop_loss": 1.24553,
             "take_profit": 1.31286, "status": "open", "source": "auto"}]})
        broker = GuardBroker([])  # post-restart: book wiped
        restored = await position_guard.rehydrate_book(db, broker)
        assert restored == 1
        _fake_live_marks["GBPUSD"] = 1.3536  # live price past TP
        out = await position_guard.guard_once(db, broker, notifier)
        assert out["closed"] == 1
        assert broker.closed == ["PAPER-000001"]
        row = db.rows["paper_trades"][0]
        assert row["close_reason"] == "tp"

    @pytest.mark.asyncio
    async def test_rehydrate_skips_tickets_already_in_book(self, notifier):
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "pt1", "ticket": "PAPER-000001", "user_id": "demo",
             "asset": "GBPUSD", "direction": "BUY", "volume": 0.01,
             "entry_price": 1.26797, "status": "open", "source": "auto"}]})
        existing = SimpleNamespace(ticket="PAPER-000001")
        broker = GuardBroker([])
        broker._pos = {}
        broker._positions = {"PAPER-000001": existing}  # already live in book
        restored = await position_guard.rehydrate_book(db, broker)
        assert restored == 0
        assert broker._positions["PAPER-000001"] is existing

    @pytest.mark.asyncio
    async def test_rehydrate_tolerates_bad_rows(self, notifier):
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "bad", "ticket": "", "status": "open"},      # no ticket
            {"id": "pt2", "ticket": "PAPER-000002", "user_id": "demo",
             "asset": "XAUUSD", "direction": "BUY", "volume": "not-a-number",
             "entry_price": None, "status": "open"},            # bad numeric
        ]})
        broker = GuardBroker([])
        restored = await position_guard.rehydrate_book(db, broker)
        assert restored <= 1  # only the valid row (or none on parse error)

    @pytest.mark.asyncio
    async def test_rehydrate_reissues_duplicate_tickets(self, notifier, _fake_live_marks):
        """Regression (prod 2026-09-03): the broker's order sequence restarts
        at 1 on every deploy, so a NEW trade re-issued a ticket identical to a
        pre-restart row still open in the DB (old GBPUSD PAPER-000001 vs new
        AUDUSD PAPER-000001) — marks and closes then hit the wrong row.
        rehydrate must re-ticket the second row and advance _seq."""
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "row-aud", "ticket": "PAPER-000001", "user_id": "demo",
             "asset": "AUDUSD", "direction": "BUY", "volume": 0.01,
             "entry_price": 0.71933, "stop_loss": 0.71566,
             "take_profit": 0.72667, "status": "open", "source": "auto"},
            {"id": "row-gbp", "ticket": "PAPER-000001", "user_id": "demo",
             "asset": "GBPUSD", "direction": "BUY", "volume": 0.01,
             "entry_price": 1.26797, "stop_loss": 1.24553,
             "take_profit": 1.31286, "status": "open", "source": "auto"},
        ]})
        broker = GuardBroker([])
        restored = await position_guard.rehydrate_book(db, broker)
        assert restored == 2
        # GBPUSD row got a fresh unique ticket in both book and DB
        gbp_row = next(r for r in db.rows["paper_trades"] if r["id"] == "row-gbp")
        assert gbp_row["ticket"] == "PAPER-000002"
        assert "PAPER-000002" in broker._positions
        assert broker._positions["PAPER-000002"].asset == "GBPUSD"
        # order sequence moved past restored tickets → no future collisions
        assert broker._seq == 2

        # and the re-issued position is enforced: live price past TP → closes,
        # and close_trade_rows updates the re-issued GBPUSD row only
        _fake_live_marks["GBPUSD"] = 1.3536
        _fake_live_marks["AUDUSD"] = 0.72  # between SL and TP → stays open
        out = await position_guard.guard_once(db, broker, notifier)
        assert out["closed"] == 1
        assert broker.closed == ["PAPER-000002"]
        assert gbp_row["status"] == "closed" and gbp_row["close_reason"] == "tp"
        aud_row = next(r for r in db.rows["paper_trades"] if r["id"] == "row-aud")
        assert aud_row["status"] == "open"


# ---------------------------------------------------------------------------
# 4. Pause state round-trip (shared by API + LINE)
# ---------------------------------------------------------------------------
class TestPauseState:
    def test_set_then_get(self):
        db = db_with_client()
        execution.set_pause(db, True, "line /pause")
        st = execution.get_pause(db)
        assert st.paused is True and st.reason == "line /pause"
        execution.set_pause(db, False, "")
        assert execution.get_pause(db).paused is False

    def test_missing_table_means_not_paused(self):
        assert execution.get_pause(FakeDatabase()).paused is False
        assert execution.get_pause(None).paused is False
