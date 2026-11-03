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
