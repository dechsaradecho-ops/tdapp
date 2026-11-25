"""signals/latest reuses the scanner's snapshot preload when fresh.

Prod 2026-11-03: every signals page load re-fetched all snapshots even
though the 5-min scanner had just preloaded the same universe — doubling
feed wait on the exact path users stare at during slow feeds. Snapshots
derive from daily candles, so a scanner preload minutes old is still a
valid gate input; spots stay live-fetched (intraday display).

Run from backend/: ..\\.venv\\Scripts\\python.exe -m pytest tests/test_signals_preload.py -q
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app.workers import market_scanner
from app.services import execution
from app.models.schemas import AppSettings
from app.integrations.quotes import TrustedQuote
from tests.test_api_routes import call, set_state
from tests.test_workers import FakeDatabase


def _snap(asset: str = "EURUSD") -> dict:
    return {"asset": asset, "price": 1.1, "ema_fast": 1.11,
            "ema_slow": 1.09, "adx": 30.0, "supertrend_dir": 1,
            "rsi": 55.0, "macd_hist": 0.1,
            "price_change_pct_20": 1.0, "atr_pct": 0.5,
            "volatility_index": 5.0, "news_sentiment": 0.0,
            "high_impact_event": False, "breakout_state": 0,
            "breakout_level": 0.0}


def _pending(asset: str = "EURUSD") -> dict:
    return {"id": "s1", "asset": asset, "direction": "buy",
            "confidence": 90.0, "entry": 1.1, "stop_loss": 1.09,
            "take_profit": 1.115, "expected_rr": 1.5,
            "approval": "pending",
            "created_at": datetime.now(timezone.utc).isoformat()}


def _db(*assets: str):
    return FakeDatabase(rows={
        "signals": [_pending(a) for a in assets],
        "trading_settings": [{"id": 1, "kill_daily_loss_pct": 60.0}],
    })


def _counting_fetches(monkeypatch):
    calls: dict[str, int] = {"snaps": 0, "spots": 0}
    seen: dict[str, list] = {"snaps": []}

    async def fake_snaps(assets, **_kw):
        calls["snaps"] += 1
        seen["snaps"].append(list(assets or []))
        return {a: _snap(a) for a in (assets or [])}

    async def fake_spots(assets, **_kw):
        calls["spots"] += 1
        return {}, {}

    import app.api.routes.signals as signals_route
    monkeypatch.setattr(signals_route.quotes, "fetch_all_snapshots",
                        fake_snaps)
    monkeypatch.setattr(signals_route.quotes, "fetch_spot_prices",
                        fake_spots)
    return calls, seen


class TestSignalsPreload:
    @pytest.mark.asyncio
    async def test_uses_fresh_scanner_preload(self, monkeypatch):
        """Scanner ran 10 s ago: route must NOT call fetch_all_snapshots."""
        import time as _t
        monkeypatch.setattr(market_scanner, "_PRELOADED_SNAPS",
                            {"EURUSD": _snap("EURUSD")})
        monkeypatch.setattr(market_scanner, "_PRELOADED_AT",
                            _t.monotonic() - 10.0)
        calls, _ = _counting_fetches(monkeypatch)
        set_state(self._db_for_test())
        body = (await call("GET", "/api/signals/latest")).json()
        assert calls["snaps"] == 0
        assert [s["asset"] for s in body] == ["EURUSD"]

    def _db_for_test(self):
        return _db("EURUSD")

    @pytest.mark.asyncio
    async def test_refetches_on_stale_preload(self, monkeypatch):
        """Preload older than the bound (or never): route fetches as before."""
        import time as _t
        monkeypatch.setattr(market_scanner, "_PRELOADED_SNAPS",
                            {"EURUSD": _snap("EURUSD")})
        monkeypatch.setattr(market_scanner, "_PRELOADED_AT",
                            _t.monotonic() - 10_000.0)
        calls, _ = _counting_fetches(monkeypatch)
        set_state(_db("EURUSD"))
        body = (await call("GET", "/api/signals/latest")).json()
        assert calls["snaps"] == 1
        assert [s["asset"] for s in body] == ["EURUSD"]

    @pytest.mark.asyncio
    async def test_fetches_only_missing_assets(self, monkeypatch):
        """Fresh preload covers EURUSD but not GBPUSD: one fetch, GBPUSD only,
        fetched snapshot wins on overlap."""
        import time as _t
        monkeypatch.setattr(market_scanner, "_PRELOADED_SNAPS",
                            {"EURUSD": _snap("EURUSD")})
        monkeypatch.setattr(market_scanner, "_PRELOADED_AT",
                            _t.monotonic() - 10.0)
        calls, seen = _counting_fetches(monkeypatch)
        set_state(_db("EURUSD", "GBPUSD"))
        body = (await call("GET", "/api/signals/latest")).json()
        assert calls["snaps"] == 1
        assert seen["snaps"] == [["GBPUSD"]]
        assert sorted(s["asset"] for s in body) == ["EURUSD", "GBPUSD"]


class TestSignalsSharedGateReads:
    """Task 3: signals/latest builds ONE SharedCycleState per request.

    The hoisted analytic reads (all-journal 500, open book 100, calendar
    50) must happen exactly once no matter how many pending cards render
    — per-card verdicts come from the shared snapshot. Safety reads
    (kill/cooldown) stay fresh per card and are NOT counted here."""

    def _counting_db(self, *assets: str):
        class _Counting(FakeDatabase):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.select_calls: list = []

            def select(self, table, filters=None, order="created_at",
                       desc=True, limit=50, offset=0, columns="*"):
                self.select_calls.append(
                    (table, tuple(sorted((filters or {}).items())), limit))
                return super().select(
                    table, filters=filters, order=order, desc=desc,
                    limit=limit, offset=offset, columns=columns)

        return _Counting(rows={
            "signals": [_pending(a) for a in assets],
            "trading_settings": [{"id": 1, "kill_daily_loss_pct": 60.0}],
        })

    @pytest.mark.asyncio
    async def test_gate_analytic_reads_stay_flat_per_card(self, monkeypatch):
        """3 pending cards render, but the analytic gate tables are read
        ONCE for the whole request (shared build), not once per card."""
        from collections import Counter
        db = self._counting_db("EURUSD", "GBPUSD", "AUDUSD")
        _counting_fetches(monkeypatch)
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert sorted(s["asset"] for s in body) == ["AUDUSD", "EURUSD", "GBPUSD"]
        counts = Counter(db.select_calls)
        assert counts[("paper_trades", (), 500)] == 1, counts
        assert counts[("paper_trades", (("status", "open"),), 100)] == 1, counts
        assert counts[("economic_calendar", (), 50)] == 1, counts


# ---------------------------------------------------------------------------
# Task 6: gold preview == execution policy (disabled / untrusted / ATR)
# ---------------------------------------------------------------------------
def _prev_settings(**over) -> AppSettings:
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


def _prev_trusted(spread: float = 0.30,
                  price: float = 3518.0585) -> TrustedQuote:
    return TrustedQuote(
        price=price, bid=price - spread / 2.0, ask=price + spread / 2.0,
        spread=spread, source="biquote", timestamp=0.0, age_seconds=0.0)


def _prev_row(direction: str, **over) -> dict:
    base = {
        "id": "sig-gold", "asset": "XAUUSD", "direction": direction,
        "entry": 3518.0585, "stop_loss": 3500.0, "take_profit": 3560.0,
        "confidence": 90.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline_supertrend_dir": 1 if direction == "BUY" else -1,
        "baseline_macd_hist": 0.1 if direction == "BUY" else -0.1,
    }
    base.update(over)
    return base


def _prev_snap(direction: str, **over) -> dict:
    buy = direction == "BUY"
    base = {
        "asset": "XAUUSD", "price": 3518.0585,
        "ema_fast": 3520.0 if buy else 3510.0,
        "ema_slow": 3510.0 if buy else 3520.0,
        "adx": 30.0,
        "supertrend_dir": 1 if buy else -1,
        "rsi": 55.0,
        "macd_hist": 0.1 if buy else -0.1,
        "price_change_pct_20": 1.0,
        "atr_pct": 1.0,
        "volatility_index": 8.0,
        "news_sentiment": 0.0,
        "high_impact_event": False,
        "breakout_state": 2 if buy else 0,
        "breakout_level": 3500.0 if buy else 0.0,
    }
    base.update(over)
    return base


class _DummyBroker:
    """Blocked-path broker: place_order must never be reached."""

    async def place_order(self, order):
        raise AssertionError("blocked gold order reached the broker")


class TestGoldPreviewParity:
    @pytest.fixture(autouse=True)
    def _offline_gold(self, monkeypatch):
        async def fake_spot(assets, **_kw):
            return {}, {}

        monkeypatch.setattr(execution.quotes, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(execution.quotes, "trusted_quote",
                            lambda asset: _prev_trusted())

    def test_preview_blocks_disabled_gold_without_snapshot(self):
        s = _prev_settings(gold_max_atr_pct=0)
        out = execution.preview_order_blocked(
            FakeDatabase(), s, asset="XAUUSD", direction="BUY",
            entry=3518.0585, stop_loss=3500.0, take_profit=3560.0,
            confidence=90.0, snapshot=None, open_assets=set())
        assert out and "gold_trading_disabled" in out

    def test_preview_blocks_untrusted_gold_without_snapshot(self, monkeypatch):
        monkeypatch.setattr(execution.quotes, "trusted_quote",
                            lambda asset: None)
        out = execution.preview_order_blocked(
            FakeDatabase(), _prev_settings(), asset="XAUUSD",
            direction="BUY", entry=3518.0585, stop_loss=3500.0,
            take_profit=3560.0, confidence=90.0, snapshot=None,
            open_assets=set())
        assert out and "gold_quote_unavailable" in out

    @pytest.mark.asyncio
    async def test_preview_matches_execution_on_gold_atr(self):
        # ATR 3.6 > 3.5: card AND execution both block (alias on execution).
        row = _prev_row("BUY")
        snap_breach = _prev_snap("BUY", atr_pct=3.6)
        out = execution.preview_order_blocked(
            FakeDatabase(), _prev_settings(), asset="XAUUSD",
            direction="BUY", entry=3518.0585, stop_loss=3500.0,
            take_profit=3560.0, confidence=90.0, signal_row=row,
            snapshot=snap_breach, open_assets=set())
        assert out != ""
        report = await execution.execute_signal(
            FakeDatabase(), _DummyBroker(), None, _prev_settings(),
            user_id="demo", asset="XAUUSD", direction="BUY",
            entry=3518.0585, stop_loss=3500.0, take_profit=3560.0,
            confidence=90.0, opportunity=90.0, signal_id="sig-gold",
            source="auto", signal_row=row, snapshot=snap_breach,
            live_price=3518.0585)
        assert not report.allowed
        assert any("gold_atr_exceeded" in r for r in report.rejects)
