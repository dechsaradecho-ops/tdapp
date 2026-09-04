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

    async def close_position(self, ticket: str) -> object:
        # manual-close endpoint contract: ok + message
        return SimpleNamespace(ok=True, broker_order_id=ticket,
                               message=f"closed {ticket} pnl=0.00")

    async def mark_price(self, ticket: str) -> float:
        return 0.0  # no book → endpoint falls back to live feed / entry

    async def quote(self, asset: str) -> float:
        return 0.0


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
