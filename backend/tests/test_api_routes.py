"""API route tests — the 3-tier data flow (DB rows → live quotes → DEMO).

Uses httpx ASGITransport with manually-populated app.state (lifespan is not
run by the transport), so each test controls what the "database" contains.

Tier contract asserted here:
  1. market_analysis/signals rows in DB win and are served as-is
  2. with no rows, live Yahoo quotes are computed per-request
  3. with no rows AND no network, deterministic DEMO data is served

Run from backend/: C:/Python314/python.exe -m pytest tests/test_api_routes.py -v
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from types import SimpleNamespace

from app.main import app
from tests.test_workers import FakeDatabase


# ---------------------------------------------------------------------------
# App/state helpers
# ---------------------------------------------------------------------------
class FakeBroker:
    async def connect(self) -> None:
        return None

    async def place_order(self, order) -> object:
        # must satisfy the broker result contract used by execution.execute_signal
        return SimpleNamespace(ok=True, broker_order_id="TCK-123", message="opened")


class FakeLine:
    async def push(self, user_id: str, message: str) -> None:
        return None

    async def reply(self, reply_token: str, message: str) -> None:
        return None


class Notifier:
    def __init__(self, db, line):
        self.db, self.line = db, line

    async def notify(self, user_id: str, ntype: str, message: str) -> None:
        return None


def set_state(db: FakeDatabase | None) -> None:
    """Populate app.state manually (ASGITransport skips lifespan)."""
    app.state.db = db if db is not None else FakeDatabase()
    app.state.line = FakeLine()
    app.state.broker = FakeBroker()


async def call(method: str, path: str, json_body: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, json=json_body)


@pytest.fixture(autouse=True)
def reset_state():
    yield
    # leave a clean FakeDatabase for any later non-fixture caller
    set_state(None)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
class TestHealth:
    @pytest.mark.asyncio
    async def test_health_ok_without_scheduler(self):
        set_state(None)
        app.state.scheduler = None
        r = await call("GET", "/health")
        body = r.json()
        assert r.status_code == 200
        assert body["status"] == "ok"
        assert body["workers"] in ("disabled", "enabled", "running")
        assert "db" in body and "ai" in body

    @pytest.mark.asyncio
    async def test_health_reports_running_when_scheduler_set(self):
        set_state(None)
        app.state.scheduler = SimpleNamespace(get_jobs=lambda: [SimpleNamespace(id="j1")])
        body = (await call("GET", "/health")).json()
        assert body["workers"] == "running"
        assert body["jobs"] == ["j1"]


# ---------------------------------------------------------------------------
# /api/market/summary — 3-tier
# ---------------------------------------------------------------------------
class TestMarketSummaryTiers:
    @pytest.mark.asyncio
    async def test_tier1_worker_rows_win(self, monkeypatch):
        """Rows in market_analysis must be served verbatim — live quotes must
        NOT be fetched (proven by making fetch raise: it must never be called)."""
        db = FakeDatabase(rows={"market_analysis": [
            {"asset": "EURUSD", "regime": "bull_trend", "sentiment": "bullish",
             "confidence": 88.0, "explanation": "worker row"},
        ]})
        set_state(db)
        from app.api.routes import market as market_route
        async def boom(assets):
            raise AssertionError("live quotes must not be fetched when rows exist")
        monkeypatch.setattr(market_route.quotes, "fetch_all_snapshots", boom)
        body = (await call("GET", "/api/market/summary")).json()
        eurusd = [o for o in body["opportunities"] if o["asset"] == "EURUSD"][0]
        assert eurusd["score"] == 88.0

    @pytest.mark.asyncio
    async def test_tier2_live_quotes_used_when_no_rows(self, monkeypatch):
        """No DB rows → live fetch must happen; served snapshot comes from it."""
        db = FakeDatabase()
        set_state(db)
        from app.api.routes import market as market_route
        from app.engine.strategy_engine import IndicatorSnapshot
        snap = IndicatorSnapshot(
            asset="XAUUSD", price=2400.0, ema_fast=2420.0, ema_slow=2380.0,
            adx=40.0, supertrend_dir=1, rsi=60.0, macd_hist=2.0,
            atr_pct=0.8, volatility_index=12.0, news_sentiment=0.0,
            high_impact_event=False, source="live")

        async def fake_fetch(assets):
            return {a: asdict(snap) for a in assets}
        monkeypatch.setattr(market_route.quotes, "fetch_all_snapshots", fake_fetch)
        body = (await call("GET", "/api/market/summary")).json()
        assert body["regime"] in ("strong_bull_trend", "bull_trend")
        assert len(body["opportunities"]) == 5

    @pytest.mark.asyncio
    async def test_tier3_demo_when_no_rows_and_no_network(self, monkeypatch):
        db = FakeDatabase()
        set_state(db)
        from app.api.routes import market as market_route
        async def boom(assets):
            raise RuntimeError("offline")
        monkeypatch.setattr(market_route.quotes, "fetch_all_snapshots", boom)
        r = await call("GET", "/api/market/summary")
        assert r.status_code == 200
        body = r.json()
        assert len(body["opportunities"]) == 5  # DEMO always has 5 assets

    @pytest.mark.asyncio
    async def test_summary_sorted_by_score_desc(self, monkeypatch):
        db = FakeDatabase(rows={"market_analysis": [
            {"asset": "AAA", "regime": "sideway", "sentiment": "neutral",
             "confidence": 10.0, "explanation": "x"},
            {"asset": "BBB", "regime": "sideway", "sentiment": "neutral",
             "confidence": 90.0, "explanation": "y"},
        ]})
        set_state(db)
        body = (await call("GET", "/api/market/summary")).json()
        scores = [o["score"] for o in body["opportunities"]]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# /api/signals/latest — 3-tier
# ---------------------------------------------------------------------------
class TestSignalsLatestTiers:
    @pytest.mark.asyncio
    async def test_tier1_db_rows_win(self, monkeypatch):
        db = FakeDatabase(rows={"signals": [
            {"asset": "XAUUSD", "direction": "buy", "confidence": 77.0,
             "entry": 1234.0, "stop_loss": 1200.0, "take_profit": 1300.0,
             "expected_rr": 2.0, "explanation": "db row"},
        ]})
        set_state(db)
        from app.api.routes import signals as signals_route
        async def boom(assets):
            raise AssertionError("live fetch must not run when DB rows exist")
        monkeypatch.setattr(signals_route.quotes, "fetch_all_snapshots", boom)
        body = (await call("GET", "/api/signals/latest")).json()
        assert len(body) == 1
        assert body[0]["asset"] == "XAUUSD"
        assert body[0]["direction"] == "BUY"
        assert body[0]["confidence"] == 77.0
        assert body[0]["reason"] == ["db row"]

    @pytest.mark.asyncio
    async def test_tier2_live_quotes_when_no_rows(self, monkeypatch):
        from app.api.routes import signals as signals_route
        from app.engine.strategy_engine import IndicatorSnapshot
        set_state(FakeDatabase())
        snap = IndicatorSnapshot(
            asset="EURUSD", price=1.10, ema_fast=1.105, ema_slow=1.09,
            adx=40.0, supertrend_dir=1, rsi=60.0, macd_hist=1.0,
            atr_pct=0.6, volatility_index=10.0, news_sentiment=0.0,
            high_impact_event=False, source="live")

        async def fake_fetch(assets):
            return {a: asdict(snap) for a in assets}
        monkeypatch.setattr(signals_route.quotes, "fetch_all_snapshots", fake_fetch)
        body = (await call("GET", "/api/signals/latest")).json()
        assert len(body) == 5
        assert all(s["recommendation"] in ("TRADE", "WAIT", "REDUCE RISK", "INCREASE CASH")
                   for s in body)

    @pytest.mark.asyncio
    async def test_tier3_demo_when_offline(self, monkeypatch):
        from app.api.routes import signals as signals_route
        set_state(FakeDatabase())
        async def boom(assets):
            raise RuntimeError("offline")
        monkeypatch.setattr(signals_route.quotes, "fetch_all_snapshots", boom)
        body = (await call("GET", "/api/signals/latest")).json()
        assert len(body) == 5  # DEMO proposals

    @pytest.mark.asyncio
    async def test_direction_uppercased_from_db(self):
        db = FakeDatabase(rows={"signals": [
            {"asset": "USDJPY", "direction": "sell", "confidence": 45.0,
             "entry": 159.0, "stop_loss": 160.0, "take_profit": 157.0,
             "expected_rr": 2.0, "explanation": "x"},
        ]})
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["direction"] == "SELL"

    @pytest.mark.asyncio
    async def test_stale_pending_expired_and_hidden(self):
        """Regression: pending signal older than TTL must leave the queue —
        the page was pinning GBPUSD at 1.26797 while the live rate was 1.35."""
        from app.services.execution import SIGNAL_TTL_MIN
        old = (datetime.now(timezone.utc)
               - timedelta(minutes=SIGNAL_TTL_MIN + 10)).isoformat()
        db = FakeDatabase(rows={"signals": [
            {"id": "s-old", "asset": "GBPUSD", "direction": "buy",
             "confidence": 80.0, "entry": 1.26797, "stop_loss": 1.26,
             "take_profit": 1.28, "expected_rr": 2.0,
             "approval": "pending", "created_at": old},
            {"id": "s-fresh", "asset": "XAUUSD", "direction": "buy",
             "confidence": 80.0, "entry": 2400.0, "stop_loss": 2350.0,
             "take_profit": 2500.0, "expected_rr": 2.0,
             "approval": "pending",
             "created_at": datetime.now(timezone.utc).isoformat()},
        ]})
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assets = [s["asset"] for s in body]
        assert "GBPUSD" not in assets           # stale row gone
        assert assets == ["XAUUSD"]             # fresh row survives
        assert db.rows["signals"][0]["approval"] == "expired"

    @pytest.mark.asyncio
    async def test_stale_approved_row_stamped_not_queued_first(self):
        """Approved rows past the TTL leave the action queue: they render at
        the bottom with an approval stamp instead of pinning a dead price at
        the top (GBPUSD 1.26797 bug)."""
        from app.services.execution import SIGNAL_TTL_MIN
        old = (datetime.now(timezone.utc)
               - timedelta(minutes=SIGNAL_TTL_MIN + 10)).isoformat()
        db = FakeDatabase(rows={"signals": [
            {"id": "s1", "asset": "GBPUSD", "direction": "buy",
             "confidence": 80.0, "entry": 1.26797, "stop_loss": 1.26,
             "take_profit": 1.28, "expected_rr": 2.0,
             "approval": "approved", "created_at": old,
             "approved_at": old},
        ]})
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["approval"] == "approved"
        assert body[0]["approved_at"] is not None

    @pytest.mark.asyncio
    async def test_fresh_approved_row_still_shown(self):
        db = FakeDatabase(rows={"signals": [
            {"id": "s1", "asset": "GBPUSD", "direction": "buy",
             "confidence": 80.0, "entry": 1.35, "stop_loss": 1.34,
             "take_profit": 1.37, "expected_rr": 2.0,
             "approval": "approved",
             "created_at": datetime.now(timezone.utc).isoformat()},
        ]})
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert len(body) == 1 and body[0]["asset"] == "GBPUSD"

    @pytest.mark.asyncio
    async def test_approved_sorted_after_pending(self):
        """Pending cards first (action queue), approved cards below them."""
        now = datetime.now(timezone.utc)
        db = FakeDatabase(rows={"signals": [
            {"id": "a1", "asset": "EURUSD", "direction": "buy",
             "confidence": 70.0, "entry": 1.16, "stop_loss": 1.15,
             "take_profit": 1.18, "expected_rr": 2.0,
             "approval": "approved", "created_at": now.isoformat(),
             "approved_at": now.isoformat()},
            {"id": "p1", "asset": "XAUUSD", "direction": "sell",
             "confidence": 75.0, "entry": 2400.0, "stop_loss": 2410.0,
             "take_profit": 2380.0, "expected_rr": 2.0,
             "approval": "pending", "created_at": now.isoformat()},
        ]})
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert [s["asset"] for s in body] == ["XAUUSD", "EURUSD"]
        assert body[0]["approval"] == "pending"
        assert body[1]["approval"] == "approved"
        assert body[1]["approved_at"] is not None


# ---------------------------------------------------------------------------
# /api/signals/approve — write path
# ---------------------------------------------------------------------------
class TestApproveFlow:
    @pytest.mark.asyncio
    async def test_reject_updates_row_and_returns_status(self):
        db = FakeDatabase(rows={"signals": [
            {"id": "sig-1", "asset": "USDJPY", "direction": "sell",
             "confidence": 45.0, "entry": 159.0, "stop_loss": 160.0,
             "take_profit": 157.0, "expected_rr": 2.0, "explanation": "x",
             "approval": "pending"},
        ]})
        set_state(db)
        r = await call("POST", "/api/signals/approve",
                       {"signal_id": "sig-1", "approve": False})
        assert r.status_code == 200
        assert r.json() == {"status": "rejected"}
        assert db.rows["signals"][0]["approval"] == "rejected"

    @pytest.mark.asyncio
    async def test_approve_executes_via_broker(self):
        db = FakeDatabase(rows={"signals": [
            {"id": "sig-2", "asset": "XAUUSD", "direction": "buy",
             "confidence": 80.0, "entry": 2400.0, "stop_loss": 2350.0,
             "take_profit": 2500.0, "expected_rr": 2.0, "explanation": "x",
             "approval": "pending"},
        ]})
        set_state(db)
        r = await call("POST", "/api/signals/approve",
                       {"signal_id": "sig-2", "approve": True})
        assert r.status_code == 200
        assert r.json()["status"] == "executed"
        assert db.rows["signals"][0]["approval"] == "approved"

    @pytest.mark.asyncio
    async def test_approve_paused_returns_blocked(self):
        """Manual kill switch must block even an approved order (Phase 1)."""
        from app.services import execution as exec_mod
        from tests.test_auto_trader import db_with_client
        db = db_with_client()
        db.rows["signals"] = [
            {"id": "sig-3", "asset": "XAUUSD", "direction": "buy",
             "confidence": 80.0, "entry": 2400.0, "stop_loss": 2350.0,
             "take_profit": 2500.0, "expected_rr": 2.0, "explanation": "x",
             "approval": "pending"}]
        set_state(db)
        exec_mod.set_pause(db, True, "test pause")
        r = await call("POST", "/api/signals/approve",
                       {"signal_id": "sig-3", "approve": True})
        assert r.status_code == 200
        assert r.json()["status"] == "blocked"
        assert any("paused" in x.lower() for x in r.json()["rejects"])
        assert db.rows["signals"][0]["approval"] == "rejected"


# ---------------------------------------------------------------------------
# /api/risk/check — pure computation
# ---------------------------------------------------------------------------
class TestRiskCheck:
    @pytest.mark.asyncio
    async def test_low_risk_when_no_drawdown(self):
        set_state(None)
        r = await call("POST", "/api/risk/check", {
            "starting_capital": 100000, "peak_equity": 100000,
            "current_equity": 100000,
        })
        body = r.json()
        assert r.status_code == 200
        assert body["risk_level"] == "low"
        assert body["trading_paused"] is False

    @pytest.mark.asyncio
    async def test_pause_at_drawdown_limit(self):
        set_state(None)
        r = await call("POST", "/api/risk/check", {
            "starting_capital": 100000, "peak_equity": 100000,
            "current_equity": 89000,  # -11% > 10% max drawdown
        })
        body = r.json()
        assert body["trading_paused"] is True
        assert body["risk_level"] == "critical"
