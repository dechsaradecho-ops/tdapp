"""API route tests — DB rows win; empty queue stays empty (no mock fallback).

Uses httpx ASGITransport with manually-populated app.state (lifespan is not
run by the transport), so each test controls what the "database" contains.

Contract asserted here:
  1. market_analysis/signals rows in DB win and are served as-is
  2. with no rows, /api/signals/latest returns [] (scanner-only cards)

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

    async def close_position(self, ticket: str) -> object:
        # manual-close endpoint contract: ok + message
        return SimpleNamespace(ok=True, broker_order_id=ticket,
                               message=f"closed {ticket} pnl=0.00")

    async def mark_price(self, ticket: str) -> float:
        return 0.0  # no book → endpoint falls back to live feed / entry

    async def quote(self, asset: str) -> float:
        return 0.0

    async def modify_stop_loss(self, ticket: str, stop_loss: float) -> object:
        return SimpleNamespace(ok=True, broker_order_id=ticket,
                               message=f"SL moved to {stop_loss:g}")

    async def modify_take_profit(self, ticket: str, take_profit: float) -> object:
        return SimpleNamespace(ok=True, broker_order_id=ticket,
                               message=f"TP moved to {take_profit:g}")


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
        from app.integrations import quotes as quotes_mod
        assert len(body["opportunities"]) == len(quotes_mod.SUPPORTED_ASSETS)

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
# /api/market/candles — position-chart popup (monitor row click)
# ---------------------------------------------------------------------------
class TestMarketCandles:
    @pytest.mark.asyncio
    async def test_returns_ohlc_oldest_first(self, monkeypatch):
        """Endpoint serves fetch_candles OHLC verbatim (oldest-first)."""
        from app.api.routes import market as market_route
        from app.integrations import quotes as quotes_mod
        bars = [quotes_mod.Candle(o=1.0 + i, h=1.1 + i, l=0.9 + i, c=1.05 + i)
                for i in range(35)]
        set_state(FakeDatabase())

        async def fake_fetch(asset, client, days=40):
            assert asset == "EURUSD"
            return bars
        monkeypatch.setattr(market_route.quotes, "fetch_candles", fake_fetch)
        body = (await call("GET", "/api/market/candles?asset=EURUSD&days=60")).json()
        assert body["asset"] == "EURUSD"
        assert body["count"] == 35
        assert body["error"] == ""
        assert body["candles"][0] == {"o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05}
        assert body["candles"][-1]["c"] == pytest.approx(1.05 + 34)

    @pytest.mark.asyncio
    async def test_fail_soft_empty_with_error(self, monkeypatch):
        """Feed failure → 200 + empty candles + error text (popup still shows
        entry/SL/TP + details without a chart)."""
        from app.api.routes import market as market_route
        from app.integrations.quotes import QuotesUnavailable
        set_state(FakeDatabase())

        async def boom(asset, client, days=40):
            raise QuotesUnavailable("feed down")
        monkeypatch.setattr(market_route.quotes, "fetch_candles", boom)
        r = await call("GET", "/api/market/candles?asset=XAUUSD")
        assert r.status_code == 200
        body = r.json()
        assert body["candles"] == [] and body["count"] == 0
        assert "feed down" in body["error"]

    @pytest.mark.asyncio
    async def test_unknown_asset_rejected(self):
        set_state(FakeDatabase())
        body = (await call("GET", "/api/market/candles?asset=FAKE")).json()
        assert body["candles"] == [] and body["count"] == 0
        assert "no feed mapping" in body["error"]


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
    async def test_no_rows_returns_empty_queue(self, monkeypatch):
        """No DB rows → empty list. No live/demo fallback: on-the-fly cards
        never passed the scanner gates and look like real tradeable signals."""
        from app.api.routes import signals as signals_route
        set_state(FakeDatabase())

        async def boom(assets):
            raise AssertionError("live fetch must not run for an empty queue")
        monkeypatch.setattr(signals_route.quotes, "fetch_all_snapshots", boom)
        monkeypatch.setattr(signals_route.quotes, "fetch_spot_prices",
                            boom)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body == []

    @pytest.mark.asyncio
    async def test_no_rows_empty_when_offline(self, monkeypatch):
        set_state(FakeDatabase())
        # Empty queue regardless of market hours — no demo fallback ever.
        body = (await call("GET", "/api/signals/latest")).json()
        assert body == []

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
        """Approved cards render FIRST (newest→oldest page order, user request
        2026-09-04): approved history above, pending action queue below."""
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
        assert [s["asset"] for s in body] == ["EURUSD", "XAUUSD"]
        assert body[0]["approval"] == "approved"
        assert body[0]["approved_at"] is not None
        assert body[1]["approval"] == "pending"

    @pytest.mark.asyncio
    async def test_cards_sorted_newest_to_oldest(self):
        """User request (2026-09-04, updated): cards render newest → oldest.
        Approved cards come first in approval order (newest first), pending
        cards follow newest-first — the newest setup is the FIRST card."""
        now = datetime.now(timezone.utc)
        db = FakeDatabase(rows={"signals": [
            {"id": "p2", "asset": "AUDUSD", "direction": "buy",
             "confidence": 75.0, "entry": 0.72, "stop_loss": 0.71,
             "take_profit": 0.74, "expected_rr": 2.0,
             "approval": "pending",
             "created_at": (now + timedelta(minutes=5)).isoformat()},
            {"id": "a1", "asset": "EURUSD", "direction": "buy",
             "confidence": 70.0, "entry": 1.16, "stop_loss": 1.15,
             "take_profit": 1.18, "expected_rr": 2.0,
             "approval": "approved", "created_at": now.isoformat(),
             "approved_at": (now + timedelta(minutes=1)).isoformat()},
            {"id": "p1", "asset": "XAUUSD", "direction": "sell",
             "confidence": 75.0, "entry": 2400.0, "stop_loss": 2410.0,
             "take_profit": 2380.0, "expected_rr": 2.0,
             "approval": "pending", "created_at": now.isoformat()},
            {"id": "a2", "asset": "GBPUSD", "direction": "buy",
             "confidence": 72.0, "entry": 1.35, "stop_loss": 1.34,
             "take_profit": 1.37, "expected_rr": 2.0,
             "approval": "approved", "created_at": now.isoformat(),
             "approved_at": (now + timedelta(minutes=3)).isoformat()},
        ]})
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert [s["asset"] for s in body] == [
            "GBPUSD", "EURUSD",   # approved, newest approval first
            "AUDUSD", "XAUUSD",   # pending, newest first
        ]

    @pytest.mark.asyncio
    async def test_pending_card_carries_order_blocked_note_at_limit(self):
        """User request (2026-09-04): signals keep generating past the limits;
        a pending card that cannot become an order right now must carry the
        reason "ไม่ได้เปิดออเดอร์นี้เพราะถึง limit แล้ว (...)"."""
        now = datetime.now(timezone.utc)
        db = FakeDatabase(rows={
            "signals": [
                {"id": "p1", "asset": "XAUUSD", "direction": "sell",
                 "confidence": 75.0, "entry": 2400.0, "stop_loss": 2410.0,
                 "take_profit": 2380.0, "expected_rr": 2.0,
                 "approval": "pending", "created_at": now.isoformat()}],
            "paper_trades": [
                {"id": f"t{i}", "asset": "EURUSD", "status": "open",
                 "created_at": now.isoformat()} for i in range(4)],
        })
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["order_blocked"], "limit hit must set order_blocked"
        assert "limit" in body[0]["order_blocked"]

    @pytest.mark.asyncio
    async def test_pending_card_no_note_when_under_limits(self):
        now = datetime.now(timezone.utc)
        db = FakeDatabase(rows={"signals": [
            {"id": "p1", "asset": "XAUUSD", "direction": "sell",
             "confidence": 75.0, "entry": 2400.0, "stop_loss": 2410.0,
             "take_profit": 2380.0, "expected_rr": 2.0,
             "approval": "pending", "created_at": now.isoformat()}],
        })
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["order_blocked"] is None

    @pytest.mark.asyncio
    async def test_pending_card_notes_open_position_on_same_asset(self):
        """User report (2026-09-04): auto mode promised "~1 นาที" forever while
        the auto-trader silently skipped the signal because the asset already
        had an open position. The card must carry that reason instead."""
        now = datetime.now(timezone.utc)
        db = FakeDatabase(rows={
            "signals": [
                {"id": "p1", "asset": "XAUUSD", "direction": "sell",
                 "confidence": 75.0, "entry": 2400.0, "stop_loss": 2410.0,
                 "take_profit": 2380.0, "expected_rr": 2.0,
                 "approval": "pending", "created_at": now.isoformat()}],
            "paper_trades": [
                {"id": "t1", "asset": "XAUUSD", "status": "open",
                 "created_at": now.isoformat()}],
        })
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["order_blocked"], (
            "open position on the same asset must set order_blocked")
        assert "XAUUSD" in body[0]["order_blocked"]
        assert "เปิดอยู่แล้ว" in body[0]["order_blocked"]

    @pytest.mark.asyncio
    async def test_card_carries_live_price_from_spot_feed(self, monkeypatch):
        """Regression (2026-09-04, "ราคาเก่า"): entries are anchored at the
        daily-close snapshot, so a card can look like a live quote while the
        market has moved. Every card must carry the CURRENT spot price
        (live_price) so the UI can show the gap next to the entry."""
        import app.api.routes.signals as signals_route

        now = datetime.now(timezone.utc)
        db = FakeDatabase(rows={"signals": [
            {"id": "p1", "asset": "XAUUSD", "direction": "sell",
             "confidence": 75.0, "entry": 2400.0, "stop_loss": 2410.0,
             "take_profit": 2380.0, "expected_rr": 2.0,
             "approval": "pending", "created_at": now.isoformat()}],
        })
        set_state(db)

        async def fake_spot(assets, **_kw):
            return {"XAUUSD": 4517.0}, {}

        monkeypatch.setattr(signals_route.quotes, "fetch_spot_prices", fake_spot)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["live_price"] == 4517.0
        assert body[0]["feed_status"]["state"] == "ok"

    @pytest.mark.asyncio
    async def test_card_live_price_none_when_feed_fails(self, monkeypatch):
        import app.api.routes.signals as signals_route

        now = datetime.now(timezone.utc)
        db = FakeDatabase(rows={"signals": [
            {"id": "p1", "asset": "XAUUSD", "direction": "sell",
             "confidence": 75.0, "entry": 2400.0, "stop_loss": 2410.0,
             "take_profit": 2380.0, "expected_rr": 2.0,
             "approval": "pending", "created_at": now.isoformat()}],
        })
        set_state(db)

        async def dead_spot(assets, **_kw):
            return {}, {"XAUUSD": "timeout"}

        monkeypatch.setattr(signals_route.quotes, "fetch_spot_prices", dead_spot)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["live_price"] is None
        assert body[0]["feed_status"]["state"] == "error"


# ---------------------------------------------------------------------------
# /api/signals/approve — write path
# ---------------------------------------------------------------------------
class TestApproveFlow:
    @pytest.fixture(autouse=True)
    def _no_network_execution_spot(self, monkeypatch):
        """Approve executes through execute_signal, which re-anchors the
        entry at the live spot price — keep that feed offline in tests."""
        from app.services import execution
        async def fake_spot(assets, **_kw):
            return {}, {}
        monkeypatch.setattr(execution.quotes, "fetch_spot_prices", fake_spot)

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


# ---------------------------------------------------------------------------
# /api/trading/positions/close — manual close from the monitor page
# ---------------------------------------------------------------------------
class TestClosePosition:
    def _open_row(self, ticket: str = "PAPER-000001", asset: str = "EURUSD",
                  direction: str = "buy", entry: float = 1.08500,
                  volume: float = 0.01) -> dict:
        return {
            "id": "row-1", "ticket": ticket, "asset": asset,
            "direction": direction, "volume": volume, "entry_price": entry,
            "stop_loss": 1.08000, "take_profit": 1.09500,
            "status": "open", "source": "auto",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @pytest.mark.asyncio
    async def test_close_returns_full_summary(self, monkeypatch):
        """Happy path: open row → close → summary with entry/exit/PnL/stats."""
        import app.api.routes.trading as trading_route

        row = self._open_row()
        db = FakeDatabase(rows={"paper_trades": [row]})
        set_state(db)

        async def fake_spot(assets, **_kw):
            return {"EURUSD": 1.09500}, {}  # +100 pips → BUY wins

        monkeypatch.setattr(trading_route, "_spot_prices", fake_spot)
        r = await call("POST", "/api/trading/positions/close",
                       {"ticket": "PAPER-000001"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["ticket"] == "PAPER-000001"
        assert body["asset"] == "EURUSD"
        assert body["direction"] == "BUY"
        assert body["entry_price"] == 1.085
        assert body["exit_price"] == 1.095
        # BUY 0.01 lot, +0.01 price → 0.01 * 0.01 * 100_000 = +10.00
        assert body["pnl"] == 10.0
        assert body["pnl_pct"] == 0.1          # 10 / 10_000 capital
        assert body["close_reason"] == "manual"
        assert body["remaining_open"] == 0
        # journal row must be closed
        assert row["status"] == "closed"
        assert row["exit_price"] == 1.095
        assert row["close_reason"] == "manual"

    @pytest.mark.asyncio
    async def test_close_sell_position_negative_pnl(self, monkeypatch):
        """SELL + price up → loss; summary carries negative PnL."""
        import app.api.routes.trading as trading_route

        row = self._open_row(direction="sell", entry=1.08500)
        db = FakeDatabase(rows={"paper_trades": [row]})
        set_state(db)

        async def fake_spot(assets, **_kw):
            return {"EURUSD": 1.09000}, {}  # +50 pips against SELL

        monkeypatch.setattr(trading_route, "_spot_prices", fake_spot)
        body = (await call("POST", "/api/trading/positions/close",
                           {"ticket": "PAPER-000001"})).json()
        assert body["ok"] is True
        assert body["pnl"] == -5.0
        assert body["pnl_pct"] == -0.05

    @pytest.mark.asyncio
    async def test_close_unknown_ticket_fails_cleanly(self):
        db = FakeDatabase(rows={"paper_trades": []})
        set_state(db)
        r = await call("POST", "/api/trading/positions/close",
                       {"ticket": "PAPER-999999"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert "ไม่พบไม้ที่เปิดอยู่" in body["message"]

    @pytest.mark.asyncio
    async def test_close_offline_falls_back_to_entry(self, monkeypatch):
        """No live feed + no broker book → exit = entry → PnL 0 (no crash)."""
        import app.api.routes.trading as trading_route

        row = self._open_row()
        db = FakeDatabase(rows={"paper_trades": [row]})
        set_state(db)

        async def dead_spot(assets, **_kw):
            return {}, {"EURUSD": "timeout"}

        monkeypatch.setattr(trading_route, "_spot_prices", dead_spot)
        body = (await call("POST", "/api/trading/positions/close",
                           {"ticket": "PAPER-000001"})).json()
        assert body["ok"] is True
        assert body["exit_price"] == 1.085
        assert body["pnl"] == 0.0

    @pytest.mark.asyncio
    async def test_close_updates_portfolio_summary(self, monkeypatch):
        """Popup stats: remaining_open, pnl_today, wins/losses after close."""
        import app.api.routes.trading as trading_route

        now = datetime.now(timezone.utc)
        closed_win = {
            "id": "row-old", "ticket": "PAPER-000001", "asset": "EURUSD",
            "direction": "buy", "volume": 0.01, "entry_price": 1.08,
            "exit_price": 1.09, "pnl": 100.0, "status": "closed",
            "close_reason": "tp", "closed_at": now.isoformat(),
            "created_at": now.isoformat(),
        }
        row = self._open_row(ticket="PAPER-000002")
        db = FakeDatabase(rows={"paper_trades": [closed_win, row]})
        set_state(db)

        async def fake_spot(assets, **_kw):
            return {"EURUSD": 1.08500}, {}  # flat → PnL 0

        monkeypatch.setattr(trading_route, "_spot_prices", fake_spot)
        body = (await call("POST", "/api/trading/positions/close",
                           {"ticket": "PAPER-000002"})).json()
        assert body["ok"] is True
        assert body["remaining_open"] == 0
        assert body["total_realized_pnl"] == 100.0
        assert body["pnl_today"] == 100.0
        assert body["wins"] == 1
        assert body["losses"] == 0


# ---------------------------------------------------------------------------
# /api/trading/positions/close-group — ปิดกำไร/ปิดขาดทุน (monitor buttons)
# ---------------------------------------------------------------------------
class TestCloseGroup:
    def _rows(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [
            {  # BUY in profit at mark 1.095
                "id": "row-1", "ticket": "PAPER-000001", "asset": "EURUSD",
                "direction": "buy", "volume": 0.01, "entry_price": 1.08500,
                "status": "open", "source": "auto",
                "created_at": now.isoformat(),
            },
            {  # BUY in loss at mark 1.095
                "id": "row-2", "ticket": "PAPER-000002", "asset": "GBPUSD",
                "direction": "buy", "volume": 0.01, "entry_price": 1.10500,
                "status": "open", "source": "auto",
                "created_at": now.isoformat(),
            },
        ]

    @pytest.mark.asyncio
    async def test_profit_group_closes_only_winners(self, monkeypatch):
        """group=profit → only the winning ticket is closed, loser untouched."""
        import app.api.routes.trading as trading_route

        rows = self._rows()
        db = FakeDatabase(rows={"paper_trades": rows})
        set_state(db)

        async def fake_spot(assets, **_kw):
            # EURUSD above entry (profit), GBPUSD below entry (loss)
            return {"EURUSD": 1.09500, "GBPUSD": 1.09500}, {}

        monkeypatch.setattr(trading_route, "_spot_prices", fake_spot)
        body = (await call("POST", "/api/trading/positions/close-group",
                           {"confirm": True, "group": "profit"})).json()
        assert body["ok"] is True
        assert body["closed"] == 1
        assert body["failed"] == 0
        assert body["results"][0]["ticket"] == "PAPER-000001"
        assert body["total_pnl"] > 0
        assert rows[0]["status"] == "closed"
        assert rows[1]["status"] == "open"   # losing row untouched

    @pytest.mark.asyncio
    async def test_loss_group_closes_only_losers(self, monkeypatch):
        """group=loss → only the losing ticket is closed (cut loss)."""
        import app.api.routes.trading as trading_route

        rows = self._rows()
        db = FakeDatabase(rows={"paper_trades": rows})
        set_state(db)

        async def fake_spot(assets, **_kw):
            return {"EURUSD": 1.09500, "GBPUSD": 1.09500}, {}

        monkeypatch.setattr(trading_route, "_spot_prices", fake_spot)
        body = (await call("POST", "/api/trading/positions/close-group",
                           {"confirm": True, "group": "loss"})).json()
        assert body["ok"] is True
        assert body["closed"] == 1
        assert body["results"][0]["ticket"] == "PAPER-000002"
        assert body["total_pnl"] < 0
        assert rows[0]["status"] == "open"   # winning row untouched
        assert rows[1]["status"] == "closed"

    @pytest.mark.asyncio
    async def test_requires_confirm(self):
        db = FakeDatabase(rows={"paper_trades": self._rows()})
        set_state(db)
        body = (await call("POST", "/api/trading/positions/close-group",
                           {"group": "profit"})).json()
        assert body["ok"] is False
        assert "confirm" in body["message"]

    @pytest.mark.asyncio
    async def test_empty_group_returns_message(self, monkeypatch):
        """No losing rows → ok with message, nothing closed."""
        import app.api.routes.trading as trading_route

        rows = [r for r in self._rows() if r["ticket"] == "PAPER-000001"]
        db = FakeDatabase(rows={"paper_trades": rows})
        set_state(db)

        async def fake_spot(assets, **_kw):
            return {"EURUSD": 1.09500}, {}  # only profit rows exist

        monkeypatch.setattr(trading_route, "_spot_prices", fake_spot)
        body = (await call("POST", "/api/trading/positions/close-group",
                           {"confirm": True, "group": "loss"})).json()
        assert body["ok"] is True
        assert body["closed"] == 0
        assert "ไม่มีไม้ที่ขาดทุน" in body["message"]

    @pytest.mark.asyncio
    async def test_no_marks_closes_nothing(self, monkeypatch):
        """Feed down → no marks → no group assignment → nothing closed."""
        import app.api.routes.trading as trading_route

        rows = self._rows()
        db = FakeDatabase(rows={"paper_trades": rows})
        set_state(db)

        async def dead_spot(assets, **_kw):
            return {}, {"EURUSD": "x", "GBPUSD": "x"}

        monkeypatch.setattr(trading_route, "_spot_prices", dead_spot)
        body = (await call("POST", "/api/trading/positions/close-group",
                           {"confirm": True, "group": "profit"})).json()
        assert body["ok"] is True
        assert body["closed"] == 0
        assert rows[0]["status"] == "open"
        assert rows[1]["status"] == "open"


# ---------------------------------------------------------------------------
# /api/trading/stats/reset — 🗑 รีเซ็ตสถิติ (monitor page)
# ---------------------------------------------------------------------------
class TestStatsReset:
    @staticmethod
    def _closed_row(row_id: str, pnl: float, **over) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "id": row_id, "ticket": f"PAPER-{row_id}", "asset": "EURUSD",
            "direction": "buy", "volume": 0.01, "entry_price": 1.08,
            "exit_price": 1.09, "pnl": pnl, "status": "closed",
            "close_reason": "tp", "closed_at": now.isoformat(),
            "created_at": now.isoformat(), **over,
        }

    @pytest.mark.asyncio
    async def test_reset_deletes_closed_keeps_open(self):
        """Happy path: closed rows deleted, open rows untouched, fresh stats."""
        db = FakeDatabase(rows={"paper_trades": [
            self._closed_row("row-c1", 100.0),
            self._closed_row("row-c2", -30.0),
            self._closed_row("row-c3", 50.0),
            {"id": "row-open", "ticket": "PAPER-open", "asset": "XAUUSD",
             "direction": "buy", "volume": 0.01, "entry_price": 2400.0,
             "status": "open", "source": "auto",
             "created_at": datetime.now(timezone.utc).isoformat()},
        ]})
        set_state(db)
        r = await call("POST", "/api/trading/stats/reset", {"confirm": True})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["deleted"] == 3
        assert "3" in body["message"]
        # open row survives
        remaining = db.rows["paper_trades"]
        assert [row["id"] for row in remaining] == ["row-open"]
        # fresh stats: everything zeroed, open position still counted
        st = body["stats"]
        assert st["closed_count"] == 0
        assert st["pnl_total"] == 0.0
        assert st["pnl_today"] == 0.0
        assert st["pnl_week"] == 0.0
        assert st["win_rate"] == 0.0
        assert st["open_positions"] == 1
        # audit trail: one signal_logs row records the reset
        resets = [row for table, row in db.inserted
                  if table == "signal_logs" and "รีเซ็ตสถิติ" in row.get("reason", "")]
        assert len(resets) == 1
        # equity history wiped + reseeded at starting capital (default 10000)
        eq = [row for table, row in db.inserted if table == "equity_snapshots"]
        assert len(eq) == 1
        assert eq[0]["equity"] == 10000.0

    @pytest.mark.asyncio
    async def test_reset_requires_confirm(self):
        """Without confirm=true the endpoint refuses (no rows deleted)."""
        db = FakeDatabase(rows={"paper_trades": [self._closed_row("row-c1", 10.0)]})
        set_state(db)
        r = await call("POST", "/api/trading/stats/reset", {})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["deleted"] == 0
        assert len(db.rows["paper_trades"]) == 1  # untouched
        # no confirm → equity history must NOT be touched either
        assert not [row for table, row in db.inserted
                    if table == "equity_snapshots"]

    @pytest.mark.asyncio
    async def test_reset_with_no_closed_rows_is_ok(self):
        """Nothing closed yet → ok, deleted=0, zeroed stats, no crash."""
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "row-open", "ticket": "PAPER-open", "asset": "EURUSD",
             "direction": "buy", "volume": 0.01, "entry_price": 1.08,
             "status": "open", "source": "auto",
             "created_at": datetime.now(timezone.utc).isoformat()}]})
        set_state(db)
        r = await call("POST", "/api/trading/stats/reset", {"confirm": True})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["deleted"] == 0
        assert body["stats"]["open_positions"] == 1
        assert body["stats"]["closed_count"] == 0

    @pytest.mark.asyncio
    async def test_reset_empty_db(self):
        db = FakeDatabase()
        set_state(db)
        body = (await call("POST", "/api/trading/stats/reset",
                           {"confirm": True})).json()
        assert body["ok"] is True and body["deleted"] == 0
        assert body["stats"]["closed_count"] == 0
        # equity history reseeded even on an empty DB
        eq = [row for table, row in db.inserted if table == "equity_snapshots"]
        assert len(eq) == 1 and eq[0]["equity"] == 10000.0

    @pytest.mark.asyncio
    async def test_reset_recomputes_stats_matching_monitor(self):
        """Fresh stats must mirror monitor_snapshot's math — a closed row
        created today counts in pnl_today/pnl_week BEFORE deletion; after
        reset everything realized is 0 while open positions remain."""
        now = datetime.now(timezone.utc)
        db = FakeDatabase(rows={"paper_trades": [
            self._closed_row("row-c1", 173.68),
            {"id": "row-open", "ticket": "PAPER-open", "asset": "EURUSD",
             "direction": "buy", "volume": 0.01, "entry_price": 1.08,
             "status": "open", "source": "auto",
             "created_at": now.isoformat()},
        ]})
        set_state(db)
        body = (await call("POST", "/api/trading/stats/reset",
                           {"confirm": True})).json()
        assert body["deleted"] == 1
        st = body["stats"]
        assert st["pnl_total"] == 0.0 and st["closed_count"] == 0
        assert st["open_positions"] == 1
        # and the monitor endpoint agrees with the reset response
        mon = (await call("GET", "/api/trading/monitor")).json()
        assert mon["stats"]["pnl_total"] == 0.0
        assert mon["stats"]["closed_count"] == 0
        assert mon["stats"]["open_positions"] == 1
        # live portfolio value: pnl = realized(0 after reset) + unrealized marks,
        # equity = capital + pnl (unrealized comes from the fake quote marks)
        unrealized = round(sum(float(p["unrealized_pnl"])
                               for p in mon["open_positions"]), 2)
        assert mon["pnl"] == unrealized
        assert mon["equity"] == round(10000.0 + unrealized, 2)


# ---------------------------------------------------------------------------
# LINE webhook — commands, AI chat replies, group mention gate + targets
# ---------------------------------------------------------------------------
import base64
import hashlib
import hmac
import json as _json
import sys
import types

from app.core.config import get_settings


class RecordingLine:
    """FakeLine that records replies so tests can assert on them."""

    enabled = True
    token = "test-token"

    def __init__(self):
        self.replies: list[tuple[str, str]] = []
        self.pushed: list[tuple[str, str]] = []

    async def push(self, user_id: str, message: str) -> bool:
        self.pushed.append((user_id, message))
        return True

    async def push_ex(self, user_id: str, message: str) -> tuple[bool, str]:
        self.pushed.append((user_id, message))
        return True, ""

    async def reply(self, reply_token: str, message: str) -> bool:
        self.replies.append((reply_token, message))
        return True


def _sign(raw: bytes) -> str:
    """LINE sends the HMAC-SHA256 digest Base64-encoded (not hex)."""
    digest = hmac.new(get_settings().line_channel_secret.encode(),
                      raw, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class TestLineWebhook:
    async def _send(self, body: dict, sig: str = "GOOD") -> httpx.Response:
        raw = _json.dumps(body).encode()
        headers = {"X-Line-Signature": sig if sig != "GOOD" else _sign(raw)}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            return await client.post("/api/line/webhook", content=raw,
                                     headers=headers)

    @pytest.mark.asyncio
    async def test_bad_signature_403(self):
        set_state(FakeDatabase())
        r = await self._send({"events": []}, sig="bad")
        assert r.status_code == 403
        # 403 leaves evidence in the debug event log (Settings panel)
        events = (await call("GET", "/api/line/events")).json()["events"]
        assert any(e["kind"] == "signature_rejected" for e in events)

    @pytest.mark.asyncio
    async def test_events_endpoint_lists_log(self):
        set_state(FakeDatabase())
        await self._send({"events": []}, sig="bad")
        r = await call("GET", "/api/line/events")
        assert r.status_code == 200
        assert any(e["kind"] == "signature_rejected" for e in r.json()["events"])

    @pytest.mark.asyncio
    async def test_simulate_command(self):
        set_state(FakeDatabase())
        body = (await call("POST", "/api/line/simulate",
                           {"text": "/risk", "source_type": "user"})).json()
        assert body["ok"] is True
        assert body["via"] == "command"
        assert "Risk" in body["reply"]

    @pytest.mark.asyncio
    async def test_simulate_free_text_uses_ai(self, monkeypatch):
        from app.api.routes import webhook as wh

        set_state(FakeDatabase())

        class FakeProvider:
            async def chat(self, messages, temperature=0.3):
                return "AI ตอบว่า: ทองน่าสนใจ"

        monkeypatch.setattr(wh, "ai_reply",
                            lambda request, text: FakeProvider().chat([]))
        body = (await call("POST", "/api/line/simulate",
                           {"text": "วันนี้ควรเทรดทองไหม"})).json()
        assert body["via"] == "ai"
        assert "ทองน่าสนใจ" in body["reply"]

    @pytest.mark.asyncio
    async def test_simulate_group_without_bot_id_skipped(self):
        """Group simulation without bot_user_id mirrors the real mention gate."""
        set_state(FakeDatabase())
        body = (await call("POST", "/api/line/simulate",
                           {"text": "hello", "source_type": "group",
                            "target_id": "C-sim"})).json()
        assert body["reply"] is None
        assert any(s["step"] == "mention_gate" and not s["ok"]
                   for s in body["steps"])

    @pytest.mark.asyncio
    async def test_simulate_group_with_bot_id_processes(self):
        set_state(FakeDatabase())
        body = (await call("POST", "/api/line/simulate",
                           {"text": "/portfolio", "source_type": "group",
                            "target_id": "C-sim",
                            "bot_user_id": "U-bot"})).json()
        assert body["reply"] and "Capital" in body["reply"]
        # group simulation registers the target (same as the real webhook)
        targets = (await call("GET", "/api/line/targets")).json()["targets"]
        assert any(t["target_id"] == "C-sim" for t in targets)

    @pytest.mark.asyncio
    async def test_simulate_empty_text_rejected(self):
        set_state(FakeDatabase())
        body = (await call("POST", "/api/line/simulate",
                           {"text": "   "})).json()
        assert body["ok"] is False
        assert body["reply"] is None

    @pytest.mark.asyncio
    async def test_wrong_bot_id_env_still_matches_mention(self, monkeypatch):
        """Regression: a stale/wrong LINE_BOT_USER_ID env must not break
        @mention matching — the webhook falls back to the real bot id from
        the LINE API (GET /bot/info)."""
        from app.api.routes import webhook as wh

        db = FakeDatabase()
        set_state(db)
        line = RecordingLine()
        app.state.line = line
        s = get_settings()
        monkeypatch.setattr(s, "line_bot_user_id", "U-wrong-env-id")

        class FakeLineInfo:
            async def get(self, url, headers=None):
                class R:
                    status_code = 200
                    def json(self):
                        return {"userId": "U-real-bot-id",
                                "displayName": "AITrade"}
                return R()

        def fake_client(*a, **kw):
            class Ctx:
                async def __aenter__(self):
                    return FakeLineInfo()
                async def __aexit__(self, *exc):
                    return False
            return Ctx()

        fake_httpx = types.SimpleNamespace(AsyncClient=fake_client)
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
        monkeypatch.setattr(wh, "_BOT_ID_CACHE", "", raising=False)

        r = await self._send({"events": [{
            "type": "message", "replyToken": "rt-9",
            "source": {"type": "group", "groupId": "C-fix", "userId": "U1"},
            "message": {"type": "text", "text": "/risk",
                        "mention": {"mentionees": [
                            {"type": "user", "userId": "U-real-bot-id"}]}},
        }]})
        assert r.status_code == 200
        assert any("Risk" in m for _, m in line.replies)
        assert wh._BOT_ID_CACHE == "U-real-bot-id"
        # mismatch between env and API value is logged as evidence
        events = (await call("GET", "/api/line/events")).json()["events"]
        assert any(e["kind"] == "bot_id_mismatch" for e in events)

    @pytest.mark.asyncio
    async def test_command_gets_canned_reply(self):
        db = FakeDatabase()
        set_state(db)
        line = RecordingLine()
        app.state.line = line
        r = await self._send({"events": [{
            "type": "message", "replyToken": "rt-1",
            "source": {"type": "user", "userId": "U1"},
            "message": {"type": "text", "text": "/risk"},
        }]})
        assert r.status_code == 200
        assert any("Risk" in m for _, m in line.replies)

    @pytest.mark.asyncio
    async def test_free_text_gets_ai_reply(self, monkeypatch):
        """Non-command text routes to the grounded AI provider."""
        from app.api.routes import webhook as wh

        db = FakeDatabase()
        set_state(db)
        line = RecordingLine()
        app.state.line = line

        class FakeProvider:
            async def chat(self, messages, temperature=0.3):
                return "AI ตอบ: วันนี้ควรรอจังหวะ"

        monkeypatch.setattr(wh, "ai_reply",
                            lambda request, text: FakeProvider().chat([]))
        r = await self._send({"events": [{
            "type": "message", "replyToken": "rt-2",
            "source": {"type": "user", "userId": "U1"},
            "message": {"type": "text", "text": "วันนี้ควรเทรดไหม"},
        }]})
        assert r.status_code == 200
        assert any("AI ตอบ" in m for _, m in line.replies)

    @pytest.mark.asyncio
    async def test_group_message_without_mention_ignored(self):
        """Group chat: no @mention → no reply (anti-spam gate)."""
        db = FakeDatabase()
        set_state(db)
        line = RecordingLine()
        app.state.line = line
        r = await self._send({"events": [{
            "type": "message", "replyToken": "rt-3",
            "source": {"type": "group", "groupId": "C-g1", "userId": "U1"},
            "message": {"type": "text", "text": "/risk"},
        }]})
        assert r.status_code == 200
        assert line.replies == []

    @pytest.mark.asyncio
    async def test_group_mention_replies_and_registers_target(self, monkeypatch):
        """@bot in a group → reply AND the group is stored in line_targets
        so future alerts are pushed there."""
        db = FakeDatabase()
        set_state(db)
        line = RecordingLine()
        app.state.line = line
        s = get_settings()
        monkeypatch.setattr(s, "line_bot_user_id", "U-bot")
        r = await self._send({"events": [{
            "type": "message", "replyToken": "rt-4",
            "source": {"type": "group", "groupId": "C-g2", "userId": "U1"},
            "message": {"type": "text", "text": "/risk",
                        "mention": {"mentionees": [
                            {"type": "user", "userId": "U-bot"}]}},
        }]})
        assert r.status_code == 200
        assert any("Risk" in m for _, m in line.replies)
        targets = db.rows.get("line_targets", [])
        assert any(t["target_id"] == "C-g2" for t in targets)

    @pytest.mark.asyncio
    async def test_target_registered_once(self):
        """Second event from the same group must not duplicate the row."""
        db = FakeDatabase()
        set_state(db)
        line = RecordingLine()
        app.state.line = line
        for _ in range(2):
            await self._send({"events": [{
                "type": "message", "replyToken": "rt-5",
                "source": {"type": "group", "groupId": "C-g3",
                           "userId": "U1"},
                "message": {"type": "text", "text": "hi"},
            }]})
        assert len(db.rows.get("line_targets", [])) == 1

    @pytest.mark.asyncio
    async def test_targets_endpoint_lists_groups(self):
        """GET /api/line/targets returns registered groups for the UI."""
        db = FakeDatabase(rows={
            "line_targets": [{"id": "lt1", "target_id": "C-g9",
                              "target_type": "group",
                              "notification_enabled": True,
                              "last_seen_at": "2026-09-05T00:00:00+00:00"}],
            "line_users": [{"id": "lu1", "user_id": "demo",
                            "line_user_id": "U-me",
                            "notification_enabled": True}],
        })
        set_state(db)
        r = await call("GET", "/api/line/targets")
        body = r.json()
        assert r.status_code == 200
        assert body["targets"][0]["target_id"] == "C-g9"
        assert body["targets"][0]["last_seen_at"] == "2026-09-05T00:00:00+00:00"
        assert body["users"][0]["line_user_id"] == "U-me"

    @pytest.mark.asyncio
    async def test_test_endpoint_pushes_to_all_targets(self):
        """POST /api/line/test pushes to every enabled target and reports
        per-target results."""
        db = FakeDatabase(rows={
            "line_targets": [
                {"id": "lt1", "target_id": "C-on", "target_type": "group",
                 "notification_enabled": True},
                {"id": "lt2", "target_id": "C-off", "target_type": "group",
                 "notification_enabled": False},
            ],
            "line_users": [{"id": "lu1", "user_id": "demo",
                            "line_user_id": "U-me",
                            "notification_enabled": True}],
        })
        set_state(db)
        line = RecordingLine()
        app.state.line = line
        r = await call("POST", "/api/line/test", {})
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is True
        assert body["sent"] == 2 and body["failed"] == 0
        pushed_to = {t for t, _ in line.pushed}
        assert pushed_to == {"C-on", "U-me"}  # disabled target skipped
        assert any("ทดสอบ" in m for _, m in line.pushed)

    @pytest.mark.asyncio
    async def test_add_target_manually(self):
        """POST /api/line/targets registers a groupId without a webhook event."""
        db = FakeDatabase()
        set_state(db)
        r = await call("POST", "/api/line/targets",
                       {"target_id": "C-manual", "target_type": "group"})
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is True
        assert any(t["target_id"] == "C-manual"
                   for t in db.rows.get("line_targets", []))

    @pytest.mark.asyncio
    async def test_add_target_rejects_bad_format(self):
        """A groupId must start with C (or R for rooms) — reject junk early."""
        db = FakeDatabase()
        set_state(db)
        r = await call("POST", "/api/line/targets", {"target_id": "hello"})
        body = r.json()
        assert body["ok"] is False
        assert db.rows.get("line_targets", []) == []

    @pytest.mark.asyncio
    async def test_add_target_idempotent(self):
        db = FakeDatabase(rows={"line_targets": [
            {"id": "lt1", "target_id": "C-dup", "target_type": "group",
             "notification_enabled": True}]})
        set_state(db)
        body = (await call("POST", "/api/line/targets",
                           {"target_id": "C-dup"})).json()
        assert body["ok"] is True
        assert "อยู่แล้ว" in body["message"]
        assert len(db.rows["line_targets"]) == 1

    @pytest.mark.asyncio
    async def test_remove_target(self):
        db = FakeDatabase(rows={"line_targets": [
            {"id": "lt1", "target_id": "C-gone", "target_type": "group",
             "notification_enabled": True}]})
        set_state(db)
        body = (await call("DELETE", "/api/line/targets/C-gone")).json()
        assert body["ok"] is True
        assert db.rows.get("line_targets", []) == []


class TestGroupNotificationPush:
    """NotificationService.push_line must reach groups (line_targets) too."""

    @pytest.mark.asyncio
    async def test_push_line_hits_users_and_groups(self):
        from app.services.notification_service import NotificationService

        db = FakeDatabase(rows={
            "line_users": [{"id": "lu1", "user_id": "demo",
                            "line_user_id": "U-personal",
                            "notification_enabled": True}],
            "line_targets": [{"id": "lt1", "target_id": "C-group",
                              "target_type": "group",
                              "notification_enabled": True}],
        })
        line = RecordingLine()
        svc = NotificationService(db, line)  # type: ignore[arg-type]
        ok = await svc.push_line("demo", "🚨 test alert")
        assert ok is True
        assert ("U-personal", "🚨 test alert") in line.pushed
        assert ("C-group", "🚨 test alert") in line.pushed

    @pytest.mark.asyncio
    async def test_push_line_disabled_target_skipped(self):
        from app.services.notification_service import NotificationService

        db = FakeDatabase(rows={
            "line_targets": [{"id": "lt1", "target_id": "C-off",
                              "target_type": "group",
                              "notification_enabled": False}],
        })
        line = RecordingLine()
        svc = NotificationService(db, line)  # type: ignore[arg-type]
        ok = await svc.push_line("demo", "msg")
        assert ok is False
        assert line.pushed == []

    @pytest.mark.asyncio
    async def test_dispatch_pending_reaches_group(self):
        from app.workers import notification_worker
        from app.services.notification_service import NotificationService

        db = FakeDatabase(rows={
            "notifications": [{"id": "n1", "user_id": "demo",
                               "channel": "line", "type": "new_signal",
                               "message": "📈 signal", "status": "pending"}],
            "line_targets": [{"id": "lt1", "target_id": "C-group",
                              "target_type": "group",
                              "notification_enabled": True}],
        })
        line = RecordingLine()
        sent = await notification_worker.dispatch_pending(
            db, NotificationService(db, line))  # type: ignore[arg-type]
        assert sent == 1
        assert ("C-group", "📈 signal") in line.pushed


# ---------------------------------------------------------------------------
# Performance dashboard — must read the LIVE system, not legacy/demo tables
# ---------------------------------------------------------------------------
class TestPerformanceSources:
    """Regression (2026-09-11, "Performance Dashboard update ให้ตรงกับระบบ
    ในปัจจุบัน"): four cards drifted off the live system —
      /correlation read legacy `trades` (migration 001, never written) → dead
      /journal read manual trading_journal (only POST /journal writes) → 0
      /paper-trading read the in-memory broker book → reset every redeploy
      /signal-report joined created.ticket (created rows never carry one) → 0
    """

    def _closed(self, rid: str, asset: str, pnl: float, sig: str,
                entry: float = 1.08000, sl: float = 1.07000,
                exit_px: float = 1.09000) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "id": rid, "ticket": f"PAPER-{rid}", "signal_id": sig,
            "asset": asset, "direction": "buy", "volume": 0.01,
            "entry_price": entry, "stop_loss": sl,
            "exit_price": exit_px, "pnl": pnl,
            "status": "closed", "source": "auto", "close_reason": "tp",
            "created_at": now, "closed_at": now,
        }

    @pytest.mark.asyncio
    async def test_correlation_reads_open_paper_trades(self):
        """Two open paper rows → both assets, nonzero forex/forex score."""
        now = datetime.now(timezone.utc).isoformat()
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "o1", "ticket": "PAPER-o1", "asset": "EURUSD",
             "direction": "buy", "volume": 0.01, "entry_price": 1.08,
             "status": "open", "created_at": now},
            {"id": "o2", "ticket": "PAPER-o2", "asset": "GBPUSD",
             "direction": "sell", "volume": 0.02, "entry_price": 1.26,
             "status": "open", "created_at": now},
        ]})
        set_state(db)
        body = (await call("GET", "/api/trading/correlation")).json()
        assert body["assets"] == ["EURUSD", "GBPUSD"]
        assert body["portfolio_correlation"] == 55.0  # forex/forex prior
        currencies = {e["currency"] for e in body["exposure"]}
        assert {"USD", "EUR", "GBP"} <= currencies

    @pytest.mark.asyncio
    async def test_correlation_empty_is_honest(self):
        """No open positions → empty assets/exposure (not a fake EURUSD/0)."""
        set_state(FakeDatabase(rows={"paper_trades": []}))
        body = (await call("GET", "/api/trading/correlation")).json()
        assert body["assets"] == []
        assert body["portfolio_correlation"] == 0.0
        assert body["exposure"] == []

    @pytest.mark.asyncio
    async def test_correlation_symbol_risk_for_requested_assets(self):
        """?assets= adds per-symbol correlation risk + cap + open book.

        The dashboard Opportunity Score needs to answer "ถ้าเปิดคู่นี้จะซ้ำ
        กับไม้ที่เปิดอยู่ไหม" without a second settings round-trip, so the
        route returns the cap it judged against and the live positions.
        """
        now = datetime.now(timezone.utc).isoformat()
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "o1", "ticket": "PAPER-o1", "asset": "EURUSD",
             "direction": "buy", "volume": 0.02, "entry_price": 1.08,
             "status": "open", "created_at": now},
        ]})
        set_state(db)
        body = (await call(
            "GET", "/api/trading/correlation?assets=EURUSD,GBPUSD,XAUUSD"
        )).json()
        assert body["open_positions"] == [
            {"asset": "EURUSD", "direction": "BUY", "volume": 0.02}]
        assert body["correlation_cap"] == 80.0  # AppSettings default
        risk = body["symbol_risk"]
        assert set(risk) == {"EURUSD", "GBPUSD", "XAUUSD"}
        # already open → duplicate/high (auto-trader's own duplicate gate)
        assert risk["EURUSD"]["duplicate"] is True
        assert risk["EURUSD"]["level"] == "high"
        # correlated forex cousin → flagged against the open EURUSD leg
        assert risk["GBPUSD"]["level"] in ("low", "medium")
        assert risk["GBPUSD"]["with"][0]["asset"] == "EURUSD"
        # gold vs forex = −0.30 prior → a hedge, NOT correlation risk
        assert risk["XAUUSD"]["with"] == []
        assert risk["XAUUSD"]["hedges"][0]["asset"] == "EURUSD"
        assert risk["XAUUSD"]["hedges"][0]["correlation"] == -0.3
        assert risk["XAUUSD"]["level"] == "none"

    @pytest.mark.asyncio
    async def test_correlation_symbol_risk_is_opt_in(self):
        """Without ?assets= the extra map stays empty (card callers unchanged)."""
        set_state(FakeDatabase(rows={"paper_trades": []}))
        body = (await call("GET", "/api/trading/correlation")).json()
        assert body["symbol_risk"] == {}
        assert body["open_positions"] == []

    @pytest.mark.asyncio
    async def test_journal_reads_closed_paper_trades(self):
        """Closed paper rows (incl. an open decoy) → win rate / PF / RR."""
        db = FakeDatabase(rows={"paper_trades": [
            self._closed("c1", "EURUSD", 10.0, "sig-1"),  # RR +1.0
            self._closed("c2", "GBPUSD", -5.0, "sig-2",
                         entry=1.26000, sl=1.25000, exit_px=1.25500),  # RR -0.5
            {"id": "o1", "ticket": "PAPER-o1", "asset": "EURUSD",
             "direction": "buy", "volume": 0.01, "entry_price": 1.08,
             "status": "open",
             "created_at": datetime.now(timezone.utc).isoformat()},
        ]})
        set_state(db)
        body = (await call("GET", "/api/trading/journal?days=30")).json()
        assert body["total_trades"] == 2
        assert body["win_rate_pct"] == 50.0
        assert body["profit_factor"] == 2.0
        assert body["average_rr"] == 0.25
        assert body["best_setup"]["pnl"] == 10.0
        assert body["worst_setup"]["pnl"] == -5.0

    @pytest.mark.asyncio
    async def test_paper_trading_survives_restart(self):
        """Broker book is EMPTY (fresh redeploy) — DB rows still drive PnL."""
        db = FakeDatabase(rows={"paper_trades": [
            self._closed("c1", "EURUSD", 10.0, "sig-1"),
            self._closed("c2", "GBPUSD", -4.0, "sig-2"),
            {"id": "o1", "ticket": "PAPER-o1", "asset": "XAUUSD",
             "direction": "buy", "volume": 0.01, "entry_price": 2400.0,
             "status": "open",
             "created_at": datetime.now(timezone.utc).isoformat()},
        ]})
        set_state(db)  # FakeBroker book: closed_trades=[] — proves DB sourcing
        body = (await call("GET", "/api/trading/paper-trading")).json()
        assert body["virtual_pnl"] == 6.0
        assert body["open_virtual_orders"] == 1
        assert body["live_readiness_score"] > 0

    @pytest.mark.asyncio
    async def test_signal_report_joins_on_signal_id(self):
        """created row carries NO ticket (scanner reality) — signal_id joins."""
        now = datetime.now(timezone.utc).isoformat()
        db = FakeDatabase(rows={
            "signal_logs": [{
                "id": "l1", "signal_id": "sig-1", "asset": "EURUSD",
                "direction": "buy", "event": "created", "confidence": 85.0,
                "ticket": "", "created_at": now}],
            "paper_trades": [self._closed("c1", "EURUSD", 12.0, "sig-1")],
            "market_analysis": [{
                "id": "m1", "asset": "EURUSD", "regime": "bull_trend",
                "sentiment": "bullish", "confidence": 85.0,
                "created_at": now}],
        })
        set_state(db)
        body = (await call("GET", "/api/trading/signal-report?days=30")).json()
        assert body["signals"] == 1
        assert body["matched_trades"] == 1
        assert body["by_asset"] == [{
            "key": "EURUSD", "trades": 1,
            "win_rate_pct": 100.0, "total_pnl": 12.0}]
        assert body["by_confidence_band"] == [{
            "key": "80-89", "trades": 1,
            "win_rate_pct": 100.0, "total_pnl": 12.0}]
        assert body["by_regime"] == [{
            "key": "bull_trend", "trades": 1,
            "win_rate_pct": 100.0, "total_pnl": 12.0}]


class TestExtendedAnalysisSync:
    """Regression (2026-09-11, "Performance Walk Forward + Extended ไม่สอดคล้อง
    กับระบบ ตรวจสอบที่มา"): extended-analysis drifted off the live system —
      market leg read limit=5 + rows[0] (newest row = arbitrary asset) →
        disagreed with the market header + goal + chat (top scorer)
      order leg priced a dummy BUY 1.0/0.99/1.02 on every asset →
        gold showed a 1.0 entry and FX-sized legs
      backtest leg was a static "POST manually" string → never a real number
    """

    def _snap(self, asset: str) -> dict:
        # strong_snapshot-shaped dict (fetch_all_snapshots contract).
        return {
            "asset": asset, "price": 100.0, "ema_fast": 105.0,
            "ema_slow": 95.0, "adx": 40.0, "supertrend_dir": 1,
            "rsi": 62.0, "macd_hist": 2.0, "atr_pct": 0.8,
            "volatility_index": 12.0, "news_sentiment": 0.0,
            "high_impact_event": False, "breakout_state": 2.0,
            "breakout_level": 99.0,
        }

    def _bars(self, n: int = 120):
        from app.integrations.quotes import Candle
        base = 1.10
        return [Candle(o=base + i * 0.0005, h=base + i * 0.0005 + 0.002,
                       l=base + i * 0.0005 - 0.002,
                       c=base + i * 0.0005 + 0.001) for i in range(n)]

    @pytest.mark.asyncio
    async def test_extended_uses_top_scorer_not_newest_row(self, monkeypatch):
        """Newest row EURUSD/sideway must NOT win — XAUUSD top scorer drives
        the order plan + live backtest leg + context."""
        import json
        from app.integrations import quotes as quotes_mod
        # FakeDatabase inserts prepend (newest first): insert the bull winner
        # FIRST so sideway EURUSD ends up newest at index 0.
        db = FakeDatabase(rows={"market_analysis": []})
        db.insert("market_analysis", {
            "asset": "XAUUSD", "regime": "strong_bull_trend",
            "sentiment": "bullish", "confidence": 78.0,
            "explanation": "t",
        })
        db.insert("market_analysis", {
            "asset": "EURUSD", "regime": "sideway", "sentiment": "neutral",
            "confidence": 42.5, "explanation": "t",
        })
        set_state(db)

        async def fake_snaps(assets, **_kw):
            return {a: dict(self._snap(a)) for a in assets}
        async def fake_spot(assets, **_kw):
            return {a: 100.0 for a in assets}, {}
        async def fake_candles(asset, client, days=120):
            return self._bars(max(30, days))
        monkeypatch.setattr(quotes_mod, "fetch_all_snapshots", fake_snaps)
        monkeypatch.setattr(quotes_mod, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(quotes_mod, "fetch_candles", fake_candles)

        r = await call("GET", "/api/trading/extended-analysis")
        assert r.status_code == 200
        body = r.json()
        plan = json.loads(body["order_strategy"])
        # top scorer wins (not the newest EURUSD row)
        assert plan["asset"] == "XAUUSD"
        # real proposal legs — never the dummy BUY 1.0/0.99/1.02 plan.
        # (lots round to 0.0 at price 100 with the FX contract value, so
        # assert on PRICES not average_entry: avg is lot-weighted → 0.0
        # when every leg rounds to 0 lots — same as production.)
        assert plan["entries"][0]["price"] == pytest.approx(100.0, abs=5.0)
        assert plan["stop_loss"] == pytest.approx(98.0, abs=5.0)
        assert plan["take_profit"] > 100.0
        assert body["execution_plan"]
        # live backtest leg names the same top scorer (not the POST hint)
        assert "XAUUSD" in body["backtest_result"]
        assert "POST" not in body["backtest_result"] or "trades" in body["backtest_result"]
        # shared grounded context with the chat surface
        assert "GROUNDED CONTEXT" in (body.get("context_block") or "")
        assert body["final_decision"]

    @pytest.mark.asyncio
    async def test_extended_asset_param_forces_selected_symbol(self, monkeypatch):
        """?asset=XXX = ประเมินคู่ที่ผู้ใช้เลือกเอง — ทุกตัวใน SUPPORTED_ASSETS
        (dropdown ORDER STRATEGY) but only real feed symbols; an unknown or
        empty value falls back to the top scorer so the box can never show a
        pair the engine cannot price."""
        import json
        from app.integrations import quotes as quotes_mod
        db = FakeDatabase(rows={"market_analysis": []})
        # newest-first inserts: XAUUSD ends up newest → top scorer = XAUUSD
        db.insert("market_analysis", {
            "asset": "EURUSD", "regime": "sideway", "sentiment": "neutral",
            "confidence": 42.5, "explanation": "t",
        })
        db.insert("market_analysis", {
            "asset": "XAUUSD", "regime": "strong_bull_trend",
            "sentiment": "bullish", "confidence": 78.0, "explanation": "t",
        })
        set_state(db)

        async def fake_snaps(assets, **_kw):
            return {a: dict(self._snap(a)) for a in assets}
        async def fake_spot(assets, **_kw):
            return {a: 100.0 for a in assets}, {}
        async def fake_candles(asset, client, days=120):
            return self._bars(max(30, days))
        monkeypatch.setattr(quotes_mod, "fetch_all_snapshots", fake_snaps)
        monkeypatch.setattr(quotes_mod, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(quotes_mod, "fetch_candles", fake_candles)

        body = (await call("GET",
                           "/api/trading/extended-analysis?asset=gbpusd")).json()
        assert body["asset"] == "GBPUSD"
        assert body["asset_source"] == "selected"
        plan = json.loads(body["order_strategy"])
        assert plan["asset"] == "GBPUSD"
        # universe = ทุกคู่ที่มีคะแนนรอบล่าสุด (ให้ dropdown ติดคะแนน)
        assert {u["asset"] for u in body["universe"]} >= {"XAUUSD", "EURUSD"}

        # สัญลักษณ์ที่ไม่ใช้ (ไม่อยู่ใน SUPPORTED_ASSETS) → ถอยไป top scorer
        bad = (await call("GET",
                          "/api/trading/extended-analysis?asset=NOPE")).json()
        assert bad["asset"] == "XAUUSD"
        assert bad["asset_source"] == "top_scorer"

        # ไม่ส่ง asset เลย = พฤติกรรมเดิม (top scorer)
        auto = (await call("GET", "/api/trading/extended-analysis")).json()
        assert auto["asset"] == "XAUUSD"
        assert auto["asset_source"] == "top_scorer"

    @pytest.mark.asyncio
    async def test_extended_offline_stays_fail_soft(self, monkeypatch):
        """No rows + dead feeds → honest EURUSD defaults, never raises."""
        from app.integrations import quotes as quotes_mod
        set_state(FakeDatabase())

        async def boom_snaps(assets, **_kw):
            raise RuntimeError("offline")
        async def boom_spot(assets, **_kw):
            raise RuntimeError("offline")
        async def boom_candles(asset, client, days=120):
            raise RuntimeError("offline")
        monkeypatch.setattr(quotes_mod, "fetch_all_snapshots", boom_snaps)
        monkeypatch.setattr(quotes_mod, "fetch_spot_prices", boom_spot)
        monkeypatch.setattr(quotes_mod, "fetch_candles", boom_candles)

        r = await call("GET", "/api/trading/extended-analysis")
        assert r.status_code == 200
        body = r.json()
        assert "EURUSD" in body["backtest_result"]
        assert "ไม่สำเร็จ" in body["backtest_result"]
        assert body["final_decision"]

    @pytest.mark.asyncio
    async def test_walk_forward_route_fail_soft_carries_provenance(self, monkeypatch):
        """Feed failure → segments 0 with asset+indicator+scope in the note."""
        from app.integrations import quotes as quotes_mod
        set_state(FakeDatabase())

        async def boom(asset, client, days=120):
            raise RuntimeError("feed down")
        monkeypatch.setattr(quotes_mod, "fetch_candles", boom)
        body = (await call("POST", "/api/trading/walk-forward", json_body={
            "asset": "EURUSD", "indicator": "EMA", "days": 120,
            "initial_capital": 10000, "risk_per_trade_pct": 1.0,
        })).json()
        assert body["segments"] == 0 and body["reliability_score"] == 0.0
        assert "EURUSD" in body["note"] and "EMA" in body["note"]
        assert "indicator-only" in body["note"]

    @pytest.mark.asyncio
    async def test_backtest_route_fail_soft_carries_provenance(self, monkeypatch):
        """Feed failure → 0 trades with asset+indicator+scope in the note."""
        from app.integrations import quotes as quotes_mod
        set_state(FakeDatabase())

        async def boom(asset, client, days=120):
            raise RuntimeError("feed down")
        monkeypatch.setattr(quotes_mod, "fetch_candles", boom)
        body = (await call("POST", "/api/trading/backtest", json_body={
            "asset": "XAUUSD", "indicator": "RSI", "days": 120,
            "initial_capital": 10000, "risk_per_trade_pct": 1.0,
        })).json()
        assert body["total_trades"] == 0
        assert "XAUUSD" in body["note"] and "RSI" in body["note"]
        assert "indicator-only" in body["note"]


# ---------------------------------------------------------------------------
# POST /api/trading/extended-open — open ONLY the first market leg
# ---------------------------------------------------------------------------
class TestExtendedOpen:
    """Safety contract (user 2026-09-11): FINAL WAIT locks the button, only
    entries[0] opens and only when it is a market leg, and the order flows
    through the single execute_signal path (journal + signal_logs + LINE)."""

    def _bars(self, n: int = 120):
        from app.integrations.quotes import Candle
        base = 1.10
        return [Candle(o=base + i * 0.0005, h=base + i * 0.0005 + 0.002,
                       l=base + i * 0.0005 - 0.002,
                       c=base + i * 0.0005 + 0.001) for i in range(n)]

    def _patch(self, monkeypatch, snap: dict):
        from app.integrations import quotes as quotes_mod

        async def fake_snaps(assets, **_kw):
            return {a: dict(snap, asset=a) for a in assets}

        async def fake_spot(assets, **_kw):
            return {a: 1.10 for a in assets}, {}

        async def fake_candles(asset, client, days=120):
            return self._bars(max(30, days))

        monkeypatch.setattr(quotes_mod, "fetch_all_snapshots", fake_snaps)
        monkeypatch.setattr(quotes_mod, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(quotes_mod, "fetch_candles", fake_candles)

    def _trend_snap(self) -> dict:
        # opportunity ~75, regime bull_trend -> FINAL TRADE, market first leg
        return {
            "asset": "EURUSD", "price": 1.10, "ema_fast": 1.12,
            "ema_slow": 1.08, "adx": 30.0, "supertrend_dir": 1,
            "rsi": 62.0, "macd_hist": 0.5, "atr_pct": 0.8,
            "volatility_index": 12.0, "news_sentiment": 0.0,
            "high_impact_event": False, "breakout_state": 0.0,
            "breakout_level": 0.0,
        }

    @pytest.mark.asyncio
    async def test_need_confirm_without_flag(self):
        set_state(FakeDatabase())
        body = (await call("POST", "/api/trading/extended-open", {})).json()
        assert body["ok"] is False and body["status"] == "need_confirm"

    @pytest.mark.asyncio
    async def test_trade_opens_first_market_leg(self, monkeypatch):
        self._patch(monkeypatch, self._trend_snap())
        db = FakeDatabase(rows={"market_analysis": [
            {"asset": "EURUSD", "regime": "bull_trend", "sentiment": "bullish",
             "confidence": 78.0, "explanation": "t"},
        ]})
        set_state(db)
        body = (await call("POST", "/api/trading/extended-open",
                           {"confirm": True})).json()
        assert body["ok"] is True, body
        assert body["status"] == "executed"
        assert body["asset"] == "EURUSD"
        assert body["final_decision"].startswith("TRADE")
        assert body["ticket"] == "TCK-123"
        # volume = the FIRST LEG's planned lot (base × 0.5 weight), NOT a
        # re-sized full-risk lot. Prod 2026-09-11: the panel showed the leg at
        # 0.01 but the order opened 0.04 because execute_signal recomputed the
        # size from the full risk budget at a tighter SL instead of using the
        # lot the user reviewed and confirmed.
        assert body["volume"] == pytest.approx(0.04, abs=0.001)
        assert body["remaining_legs"] == 2
        opens = db.rows.get("paper_trades", [])
        assert len(opens) == 1 and opens[0]["source"] == "extended"
        assert opens[0]["volume"] == pytest.approx(body["volume"])
        assert body.get("warnings") == []
        logs = db.rows.get("signal_logs", [])
        assert any(r.get("event") == "order_opened"
                   and r.get("source") == "extended" for r in logs)
        notes = db.rows.get("notifications", [])
        assert any(r.get("type") == "trade_opened" for r in notes)

    @pytest.mark.asyncio
    async def test_opened_volume_equals_plan_leg_lot(self, monkeypatch):
        """The executed lot must equal the lot of the leg the panel showed."""
        import json as _json
        self._patch(monkeypatch, self._trend_snap())
        db = FakeDatabase(rows={"market_analysis": [
            {"asset": "EURUSD", "regime": "bull_trend", "sentiment": "bullish",
             "confidence": 78.0, "explanation": "t"},
        ]})
        set_state(db)
        plan_body = (await call("GET", "/api/trading/extended-analysis")).json()
        plan = _json.loads(str(plan_body.get("order_strategy") or "{}"))
        first = dict(plan["entries"][0])
        assert first["order_type"] == "market"
        body = (await call("POST", "/api/trading/extended-open",
                           {"confirm": True})).json()
        assert body["ok"] is True, body
        assert body["volume"] == pytest.approx(float(first["lot"]), abs=1e-6)
        assert db.rows["paper_trades"][0]["volume"] == pytest.approx(
            float(first["lot"]))

    @pytest.mark.asyncio
    async def test_zero_lot_plan_blocks_instead_of_re_sizing(self, monkeypatch):
        """A plan leg of 0 lots must BLOCK, never silently re-size.

        Falling back to risk sizing here is exactly the reported bug: the
        panel showed lot 0 (risk budget too small for 2dp) and the order
        opened 0.04 for the full risk budget.
        """
        self._patch(monkeypatch, self._trend_snap())
        from app.models import schemas as _schemas
        _real = _schemas.OrderStrategyEngine.build_plan

        def _zero_leg(engine_self, **kw):
            p = _real(engine_self, **kw)
            p.entries[0].lot = 0.0
            return p

        monkeypatch.setattr(_schemas.OrderStrategyEngine, "build_plan", _zero_leg)
        db = FakeDatabase(rows={"market_analysis": [
            {"asset": "EURUSD", "regime": "bull_trend", "sentiment": "bullish",
             "confidence": 78.0, "explanation": "t"},
        ]})
        set_state(db)
        body = (await call("POST", "/api/trading/extended-open",
                           {"confirm": True})).json()
        assert body["ok"] is False and body["status"] == "blocked"
        assert any("lot 0" in str(r) for r in body.get("rejects", []))
        assert db.rows.get("paper_trades", []) == []
        logs = db.rows.get("signal_logs", [])
        assert any(r.get("event") == "order_blocked"
                   and r.get("source") == "extended" for r in logs)

    @pytest.mark.asyncio
    async def test_wait_blocks_and_logs(self, monkeypatch):
        # choppy snapshot -> opportunity ~55 < 70 -> FINAL WAIT, never executes
        snap = dict(self._trend_snap(), ema_fast=1.1004, ema_slow=1.0996,
                    adx=12.0, supertrend_dir=0, rsi=50.0, macd_hist=0.1,
                    atr_pct=0.5, volatility_index=8.0)
        self._patch(monkeypatch, snap)
        db = FakeDatabase(rows={"market_analysis": [
            {"asset": "EURUSD", "regime": "sideway", "sentiment": "neutral",
             "confidence": 42.5, "explanation": "t"},
        ]})
        set_state(db)
        body = (await call("POST", "/api/trading/extended-open",
                           {"confirm": True})).json()
        assert body["ok"] is False and body["status"] == "blocked"
        assert body["final_decision"].startswith("WAIT")
        assert db.rows.get("paper_trades", []) == []
        logs = db.rows.get("signal_logs", [])
        assert any(r.get("event") == "order_blocked"
                   and r.get("source") == "extended" for r in logs)

    @pytest.mark.asyncio
    async def test_sideway_first_leg_not_market_blocked(self, monkeypatch):
        # adx 15 -> regime sideway (breakout stops, no market leg) but
        # opportunity ~75 -> FINAL TRADE -> blocked on the leg check, honestly
        snap = dict(self._trend_snap(), adx=15.0, rsi=50.0,
                    volatility_index=8.0)
        self._patch(monkeypatch, snap)
        db = FakeDatabase(rows={"market_analysis": [
            {"asset": "EURUSD", "regime": "sideway", "sentiment": "neutral",
             "confidence": 80.0, "explanation": "t"},
        ]})
        set_state(db)
        body = (await call("POST", "/api/trading/extended-open",
                           {"confirm": True})).json()
        assert body["ok"] is False and body["status"] == "blocked"
        assert body["final_decision"].startswith("TRADE")
        assert any("market" in str(r) for r in body.get("rejects", []))
        assert db.rows.get("paper_trades", []) == []
