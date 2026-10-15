"""Preview/execution parity — /signals/latest order_blocked must match the real gate.

Prod 2026-10-15: ~7 pending cards showed no order_blocked ("พร้อมยิง") but the
auto-trader blocked every one at execution (meta_filter / thesis regime /
kill / min-lot budget ...). The preview in signals.py hand-maintained a
SUBSET of the execution pipeline (duplicate/cooldown/heat/limits/pre_open/
exposure) while execute_signal runs the FULL pipeline — any card passing the
subset but failing the rest promised "~1 นาที" forever.

Contract asserted here: whatever blocks execute_signal must already show on
the card (same verdict, same first-reason ordering).

Run from backend/: .\\..\\.venv\\Scripts\\python.exe -m pytest tests/test_preview_parity.py -q
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services import execution
from tests.test_api_routes import call, set_state
from tests.test_workers import FakeDatabase


async def _no_spot(assets, **_kw):
    return {}, {}


def _pin_market_open(monkeypatch):
    import app.api.routes.signals as signals_route
    monkeypatch.setattr(signals_route.quotes, "fetch_spot_prices", _no_spot)
    monkeypatch.setattr(execution, "is_market_closed",
                        lambda *a, **k: False)


def _pending(asset: str = "EURUSD", direction: str = "buy",
             confidence: float = 90.0, entry: float = 1.1,
             sl: float = 1.09, tp: float = 1.115) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {"id": "p1", "asset": asset, "direction": direction,
            "confidence": confidence, "entry": entry, "stop_loss": sl,
            "take_profit": tp, "expected_rr": 1.5,
            "approval": "pending", "created_at": now}


class TestPreviewParity:
    @pytest.mark.asyncio
    async def test_preview_reports_market_closed(self, monkeypatch):
        """Gate 0b (weekend close) blocks execution FIRST, but the old preview
        subset had no market-closed check — the card showed ready on Saturday."""
        import app.api.routes.signals as signals_route
        monkeypatch.setattr(signals_route.quotes, "fetch_spot_prices",
                            _no_spot)
        monkeypatch.setattr(execution, "is_market_closed",
                            lambda *a, **k: True)
        monkeypatch.setattr(execution, "session_filter_block",
                            lambda s: "")
        db = FakeDatabase(rows={
            "signals": [_pending()],
            "trading_settings": [{"id": 1, "kill_daily_loss_pct": 60.0}],
        })
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["order_blocked"] is not None, (
            "market closed must already show on the card")
        assert "ตลาดปิด" in body[0]["order_blocked"]

    @pytest.mark.asyncio
    async def test_preview_reports_thesis_flip(self, monkeypatch):
        """Snapshot gates sync too: a BUY card against a live bearish EMA
        must carry thesis_error before execution does."""
        import app.api.routes.signals as signals_route

        async def bear_snapshot(assets, **_kw):
            return {a: {"asset": a, "price": 1.1, "ema_fast": 1.09,
                        "ema_slow": 1.11, "adx": 30.0, "supertrend_dir": -1,
                        "rsi": 40.0, "macd_hist": -0.5,
                        "price_change_pct_20": -1.0, "atr_pct": 0.5,
                        "volatility_index": 12.0, "news_sentiment": 0.0,
                        "high_impact_event": False, "breakout_state": 0,
                        "breakout_level": 0.0}
                    for a in assets}

        monkeypatch.setattr(signals_route.quotes, "fetch_all_snapshots",
                            bear_snapshot)
        _pin_market_open(monkeypatch)
        monkeypatch.setattr(execution, "session_filter_block",
                            lambda s: "")
        db = FakeDatabase(rows={
            "signals": [_pending()],
            "trading_settings": [{"id": 1, "kill_daily_loss_pct": 60.0}],
        })
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["order_blocked"] is not None, (
            "thesis flip must already show on the card")
        assert "thesis_error" not in body[0]["order_blocked"]
        assert "regime_blocked" not in body[0]["order_blocked"]
        assert "EMA" in body[0]["order_blocked"]

    @pytest.mark.asyncio
    async def test_preview_reports_min_lot_budget_breach(self, monkeypatch):
        """P0-2: a $100 account cannot fund the 0.01 floor on a 0.01-wide SL
        ($10 risk vs $2 budget) — execution BLOCKS, so the card must say so."""
        _pin_market_open(monkeypatch)
        monkeypatch.setattr(execution, "session_filter_block",
                            lambda s: "")
        db = FakeDatabase(rows={
            "signals": [_pending(entry=1.1, sl=1.09, tp=1.115)],
            "trading_settings": [{"id": 1, "capital": 100.0,
                                  "risk_per_trade_pct": 2.0,
                                  "kill_daily_loss_pct": 60.0,
                                  "sl_cap_enabled": False}],
        })
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert body[0]["order_blocked"] is not None, (
            "min-lot budget breach must already show on the card")
        assert "งบต่อไม้" in body[0]["order_blocked"]

    @pytest.mark.asyncio
    async def test_preview_allows_jpy_when_converted(self, monkeypatch):
        """CHFJPY SELL 190.24/190.33 on $500/2%: true risk ~$0.57 — the card
        must NOT trip the min-lot budget gate (legacy read the ¥90 quote
        figure as $90 = 18% > 2%). Prod-faithful: the spot mock echoes ONLY
        requested assets (no unconditional USDJPY gift), so the card converts
        only because the route asked for the USDJPY leg in the same batch.
        Display math (suggested_lots + risk note) must use the same map."""
        import app.api.routes.signals as signals_route

        requested: list[str] = []
        _TABLE = {"CHFJPY": 190.24, "USDJPY": 157.9}

        async def _echo_spot(assets, **_kw):
            requested.extend([str(a).upper() for a in (assets or [])])
            return ({str(a).upper(): _TABLE[str(a).upper()]
                     for a in (assets or [])
                     if str(a).upper() in _TABLE}, {})

        async def _empty_snapshots(assets, **_kw):
            return {}

        monkeypatch.setattr(signals_route.quotes, "fetch_spot_prices",
                            _echo_spot)
        monkeypatch.setattr(signals_route.quotes, "fetch_all_snapshots",
                            _empty_snapshots)
        monkeypatch.setattr(execution, "is_market_closed",
                            lambda *a, **k: False)
        monkeypatch.setattr(execution, "session_filter_block",
                            lambda s: "")
        db = FakeDatabase(rows={
            "signals": [_pending(asset="CHFJPY", direction="sell",
                                 confidence=90.0, entry=190.24,
                                 sl=190.33, tp=190.105)],
            "trading_settings": [{"id": 1, "capital": 500.0,
                                  "risk_per_trade_pct": 2.0,
                                  "kill_daily_loss_pct": 60.0}],
        })
        set_state(db)
        body = (await call("GET", "/api/signals/latest")).json()
        assert "USDJPY" in requested, (
            "route must request the USDJPY conversion leg for a lone-CHFJPY "
            f"page (asked for: {sorted(set(requested))})")
        assert body[0]["order_blocked"] is None, (
            f"converted JPY risk must not block the card: "
            f"{body[0]['order_blocked']!r}")
        assert "งบต่อไม้" not in str(body[0]["order_blocked"] or "")
        # I1: display math uses the same converted map (legacy sized 0.01
        # lots / $90.00 risk on this card).
        assert body[0]["suggested_lots"] == pytest.approx(0.18, abs=0.02), (
            f"converted sizing must show ~0.18 lots: "
            f"{body[0]['suggested_lots']!r}")
        _notes = " ".join(body[0].get("calc_notes") or [])
        assert "$10.26" in _notes, (
            f"risk note must show the converted $10.26 (2.05%), not ¥-as-$: "
            f"{_notes!r}")
        assert "$90.00" not in _notes, (
            f"legacy ¥-as-$ figure must be gone: {_notes!r}")
