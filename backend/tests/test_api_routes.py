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
        # The closed-market guard sits before the live-quote tier too.
        monkeypatch.setattr(signals_route, "is_market_closed",
                            lambda now=None: False)
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
        from app.models.schemas import is_market_closed
        set_state(FakeDatabase())
        # The demo fallback is skipped during the weekend close — force open.
        monkeypatch.setattr(signals_route, "is_market_closed",
                            lambda now=None: False)
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


# ---------------------------------------------------------------------------
# LINE webhook — commands, AI chat replies, group mention gate + targets
# ---------------------------------------------------------------------------
import base64
import hashlib
import hmac
import json as _json

from app.core.config import get_settings


class RecordingLine:
    """FakeLine that records replies so tests can assert on them."""

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
