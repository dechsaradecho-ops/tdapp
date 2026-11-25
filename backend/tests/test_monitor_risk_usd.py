"""Monitor risk_amount must be real USD for every supported asset.

Production bug (execution.monitor_snapshot): ``risk_usd_of_distance`` was
called WITHOUT ``rates=pnl_rates``, so any pair whose quote leg is not
derivable from the pair itself (GBPJPY and every other JPY-quote cross,
plus non-USD-quoted crosses like AUDCHF/USDCAD/...) fell back to the RAW
quote-currency figure mislabeled as USD:

    GBPJPY SELL 0.02, entry 207.918, SL 210.02272
    → risk_amount $4209.44 (raw JPY) instead of ≈$26.43 at USDJPY≈159.29.

Contract under test (through the real HTTP monitor, not helper internals):
  * risk_amount == SL distance × lots × contract, converted quote→USD.
  * when the conversion leg is unavailable → risk_amount == 0.0 plus an
    honest calc note (never a wrong-currency $ figure).
  * R math / uPnL behaviour unchanged (spot-checked on the repro case).

Run from backend/: python -m pytest tests/test_monitor_risk_usd.py -q
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.integrations import quotes as quotes_mod
from app.main import app
from app.services import execution as execution_mod
from tests.test_workers import FakeDatabase


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _FakeBroker:
    async def all_positions(self):
        return []


class _FakeLine:
    async def push(self, user_id: str, message: str) -> None:
        return None

    async def reply(self, reply_token: str, message: str) -> None:
        return None


def _set_state(db: FakeDatabase) -> None:
    app.state.db = db
    app.state.line = _FakeLine()
    app.state.broker = _FakeBroker()


async def _get_monitor() -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/trading/monitor")
    assert resp.status_code == 200, resp.text
    return resp.json()


# Static spot board the fake feed serves (trade assets AND conversion legs).
USDJPY = 159.29
SPOTS: dict[str, float] = {
    "EURUSD": 1.0850, "GBPUSD": 1.2700, "USDJPY": USDJPY,
    "AUDUSD": 0.6600, "NZDUSD": 0.6000, "USDCAD": 1.4300,
    "USDCHF": 0.8900,
    "EURGBP": 0.8543, "EURJPY": 172.83, "EURAUD": 1.6439,
    "EURNZD": 1.8083, "EURCAD": 1.5516, "EURCHF": 0.9657,
    "GBPJPY": 207.918, "GBPAUD": 1.9242, "GBPNZD": 2.1167,
    "GBPCAD": 1.8161, "GBPCHF": 1.1303,
    "AUDJPY": 105.13, "AUDNZD": 1.1000, "AUDCAD": 0.9438,
    "AUDCHF": 0.5874, "NZDJPY": 95.57, "NZDCAD": 0.8580,
    "CADJPY": 111.39, "CADCHF": 0.6224, "CHFJPY": 178.97,
    "XAUUSD": 2650.0,
    # conversion legs (<quote>USD) served by the same fake feed
    "JPYUSD": 1.0 / USDJPY, "CHFUSD": 1.0 / 0.8900,
    "CADUSD": 1.0 / 1.4300,
    "EURUSD_leg": 1.0850, "GBPUSD_leg": 1.2700,
    "AUDUSD_leg": 0.6600, "NZDUSD_leg": 0.6000,
}

# USD value of ONE unit of each quote currency (independent constants —
# deliberately NOT imported from the helper under test).
QUOTE_USD = {"USD": 1.0, "JPY": 1.0 / USDJPY, "CHF": 1.0 / 0.8900,
             "CAD": 1.0 / 1.4300, "GBP": 1.2700, "EUR": 1.0850,
             "AUD": 0.6600, "NZD": 0.6000, "XAU": 1.0}

CONTRACT = {"XAUUSD": 100.0}


def _stub_feed(monkeypatch, spots: dict[str, float]) -> None:
    async def fake_spot(assets, **_kw):
        return ({a: spots[a] for a in (assets or []) if a in spots}, {})
    monkeypatch.setattr(execution_mod.quotes, "fetch_spot_prices",
                        fake_spot)
    monkeypatch.setattr(execution_mod.quotes, "fetch_all_snapshots",
                        lambda assets, **_kw: _empty_snaps(assets))

    async def _empty_snaps(assets, **_kw):
        return {}

    async def fake_display(assets, **_kw):
        from app.integrations.quotes import DisplayQuote
        prices, meta, failures = {}, {}, {}
        for a in (assets or []):
            key = str(a).upper()
            if key not in spots:
                # Unknown leg, not a silent 1.0: the monitor must fail
                # closed on conversion exactly like the spot stub below.
                failures[key] = f"{key}: no feed"
                continue
            px = spots[key]
            prices[key] = px
            meta[key] = DisplayQuote(price=px, source="yahoo",
                                     age_seconds=1.0, market_state="open",
                                     stale=False)
        return prices, meta, failures
    monkeypatch.setattr(execution_mod.quotes, "fetch_display_prices",
                        fake_display)


def _open_row(asset: str, entry: float, sl: float,
              volume: float = 0.01, direction: str = "BUY") -> dict:
    return {
        "id": f"row-{asset}", "ticket": f"PAPER-{asset}", "asset": asset,
        "direction": direction, "volume": volume, "entry_price": entry,
        "stop_loss": sl, "take_profit": None,
        "status": "open", "source": "auto",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# (asset, entry, stop_loss, volume) — entry/SL sit on the SPOTS board.
CASES: list[tuple[str, float, float, float]] = [
    ("EURUSD", 1.0850, 1.0800, 0.01),     # USD-quoted
    ("GBPUSD", 1.2700, 1.2650, 0.01),     # USD-quoted
    ("AUDUSD", 0.6600, 0.6550, 0.01),     # USD-quoted
    ("NZDUSD", 0.6000, 0.5950, 0.01),     # USD-quoted
    ("USDJPY", 159.29, 158.79, 0.01),     # USD-base
    ("USDCAD", 1.4300, 1.4250, 0.01),     # USD-base, non-USD quote
    ("USDCHF", 0.8900, 0.8850, 0.01),     # USD-base, non-USD quote
    ("EURGBP", 0.8543, 0.8493, 0.01),     # non-USD cross
    ("EURJPY", 172.83, 172.33, 0.01),     # JPY-quote cross
    ("EURAUD", 1.6439, 1.6389, 0.01),     # non-USD cross
    ("EURNZD", 1.8083, 1.8033, 0.01),     # non-USD cross
    ("EURCAD", 1.5516, 1.5466, 0.01),     # non-USD cross
    ("EURCHF", 0.9657, 0.9607, 0.01),     # non-USD cross
    ("GBPJPY", 207.918, 210.02272, 0.02),  # prod repro (SELL below)
    ("GBPAUD", 1.9242, 1.9192, 0.01),
    ("GBPNZD", 2.1167, 2.1117, 0.01),
    ("GBPCAD", 1.8161, 1.8111, 0.01),
    ("GBPCHF", 1.1303, 1.1253, 0.01),
    ("AUDJPY", 105.13, 104.63, 0.01),     # JPY-quote cross
    ("AUDNZD", 1.1000, 1.0950, 0.01),
    ("AUDCAD", 0.9438, 0.9388, 0.01),     # non-USD cross
    ("AUDCHF", 0.5874, 0.5824, 0.01),     # non-USD cross
    ("NZDJPY", 95.57, 95.07, 0.01),       # JPY-quote cross
    ("NZDCAD", 0.8580, 0.8530, 0.01),
    ("CADJPY", 111.39, 110.89, 0.01),     # JPY-quote cross
    ("CADCHF", 0.6224, 0.6174, 0.01),     # non-USD cross
    ("CHFJPY", 178.97, 178.47, 0.01),     # JPY-quote cross, CHF base
    ("XAUUSD", 2650.0, 2645.0, 0.01),     # gold (100 oz contract)
]


def _expected_usd(asset: str, entry: float, sl: float,
                  volume: float) -> float:
    base, quote = asset[:3], asset[3:]
    if asset == "XAUUSD":
        base, quote = "XAU", "USD"
    contract = CONTRACT.get(asset, 100_000.0)
    raw = abs(entry - sl) * volume * contract
    rate = QUOTE_USD[quote] if base != "USD" else 1.0 / entry
    return round(raw * rate, 2)


@pytest.mark.asyncio
async def test_monitor_risk_amount_is_real_usd_for_every_asset(
        monkeypatch) -> None:
    """Table-driven audit: all 27 FX pairs + XAUUSD convert SL risk to USD."""
    assert len(CASES) == 28, "audit must cover every SUPPORTED_ASSETS pair"
    assert sorted(c[0] for c in CASES) == sorted(quotes_mod.SUPPORTED_ASSETS)
    execution_mod._conv_rates_cache.clear()
    _stub_feed(monkeypatch, SPOTS)
    for asset, entry, sl, volume in CASES:
        direction = "SELL" if sl > entry else "BUY"
        db = FakeDatabase(rows={"paper_trades": [
            _open_row(asset, entry, sl, volume, direction)]})
        _set_state(db)
        body = await _get_monitor()
        assert len(body["open_positions"]) == 1, asset
        pos = body["open_positions"][0]
        expected = _expected_usd(asset, entry, sl, volume)
        assert pos["risk_amount"] == pytest.approx(expected, abs=0.02), (
            f"{asset}: risk_amount {pos['risk_amount']} != "
            f"expected USD {expected} (raw quote figure leaked as $?)")
        # the step-by-step note must show the converted $ figure too
        assert any("$" in n and "SL" in n for n in pos["calc_notes"]), asset


@pytest.mark.asyncio
async def test_monitor_risk_amount_prod_repro_gbpjpy(monkeypatch) -> None:
    """Exact production repro: GBPJPY SELL 0.02 must read ≈$26.43, not $4209.44."""
    execution_mod._conv_rates_cache.clear()
    _stub_feed(monkeypatch, SPOTS)
    db = FakeDatabase(rows={"paper_trades": [
        _open_row("GBPJPY", 207.918, 210.02272, 0.02, "SELL")]})
    _set_state(db)
    pos = (await _get_monitor())["open_positions"][0]
    assert pos["risk_amount"] == pytest.approx(26.43, abs=0.05)
    assert pos["risk_amount"] < 100  # guards the 157× ¥-as-$ regression
    # R math untouched: mark == entry here, so R must be exactly 0
    assert pos["r_multiple"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_monitor_risk_amount_fail_closed_without_leg(
        monkeypatch) -> None:
    """No conversion leg → risk_amount 0.0 + honest note, never raw ¥ as $."""
    execution_mod._conv_rates_cache.clear()
    # feed serves the trade asset only — JPYUSD/USD/GBP legs stay unknown
    _stub_feed(monkeypatch, {"GBPJPY": 207.918})
    db = FakeDatabase(rows={"paper_trades": [
        _open_row("GBPJPY", 207.918, 210.02272, 0.02, "SELL")]})
    _set_state(db)
    pos = (await _get_monitor())["open_positions"][0]
    assert pos["risk_amount"] == 0.0
    assert any("แปลงสกุลเงินไม่สำเร็จ" in n for n in pos["calc_notes"])
