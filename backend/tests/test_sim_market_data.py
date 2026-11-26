"""Provider adapters + validation for sim price data V2 (no live network).

Run from backend/: pytest tests/test_sim_market_data.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.services.sim_market_data import (
    DatasetBuildReport,
    SimBar,
    fetch_biquote_recent,
    fetch_twelvedata_history,
    hash_bars,
    merge_history,
    twelvedata_symbol,
    validate_sim_bar,
)
from app.services.price_history import today_utc


def bar(bar_date: str, close: float) -> SimBar:
    return SimBar("EURUSD", bar_date, 1.10, 1.11, 1.09, close, "biquote", None)


def _sample_bars() -> list[SimBar]:
    return [
        SimBar("EURUSD", "2026-01-02", 1.19, 1.21, 1.18, 1.20, "biquote", None),
        SimBar("EURUSD", "2026-01-01", 1.09, 1.11, 1.08, 1.10, "twelvedata", None),
    ]


def test_biquote_wins_overlap_and_reports_discrepancy():
    bq = [bar("2026-01-02", close=1.20)]
    td = [bar("2026-01-01", close=1.10), bar("2026-01-02", close=1.21)]
    merged, discrepancies = merge_history("EURUSD", bq, td)
    assert [b.bar_date for b in merged] == ["2026-01-01", "2026-01-02"]
    assert merged[-1].close == 1.20
    assert discrepancies[0]["bar_date"] == "2026-01-02"


def test_hash_is_canonical_and_stable():
    bars = _sample_bars()
    assert hash_bars(bars) == hash_bars(list(reversed(bars)))
    assert hash_bars(bars) == hash_bars(bars)


def _biquote_transport() -> httpx.MockTransport:
    today = datetime.now(timezone.utc).date()
    bars = []
    for i in range(500):
        day = today - timedelta(days=500 - i)
        base = 1.1000 + i * 0.0001
        bars.append({
            "openTime": f"{day.isoformat()}T00:00:00Z",
            "open": base,
            "high": base + 0.0010,
            "low": base - 0.0010,
            "close": base + 0.0005,
            "tickVolume": 100,
            "isOpen": i == 499,  # newest bar still forming -> dropped
        })

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/EURUSD/ohlc" in str(request.url)
        return httpx.Response(200, json={"bars": bars})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_biquote_recent_drops_open_bar_and_marks_source():
    async with httpx.AsyncClient(transport=_biquote_transport()) as client:
        bars = await fetch_biquote_recent("EURUSD", client)
    assert len(bars) == 499
    assert all(b.source == "biquote" for b in bars)
    assert bars[-1].bar_date < today_utc()


def _td_transport(captured: dict) -> httpx.MockTransport:
    values = [
        {"datetime": "2021-01-05 00:00:00", "open": "140.10",
         "high": "140.50", "low": "139.80", "close": "140.20"},
        {"datetime": "2021-01-04 00:00:00", "open": "139.90",
         "high": "140.30", "low": "139.60", "close": "140.00"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured["symbol"] = request.url.params.get("symbol")
        captured["interval"] = request.url.params.get("interval")
        captured["outputsize"] = request.url.params.get("outputsize")
        return httpx.Response(200, json={"status": "ok", "values": values})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_twelvedata_history_maps_fx_symbol_and_parses_values(monkeypatch):
    import app.services.sim_market_data as smd

    monkeypatch.setattr(
        smd, "get_settings",
        lambda: SimpleNamespace(twelvedata_api_key="TESTKEY"),
    )
    captured: dict = {}
    async with httpx.AsyncClient(transport=_td_transport(captured)) as client:
        bars = await fetch_twelvedata_history("GBPJPY", years=5, client=client)
    requested_symbol = captured["symbol"]
    assert requested_symbol == "GBP/JPY"
    assert captured["interval"] == "1day"
    assert all(b.source == "twelvedata" for b in bars)
    assert [b.bar_date for b in bars] == ["2021-01-04", "2021-01-05"]
    assert bars[0].close == pytest.approx(140.00)


def test_twelvedata_symbol_mapping():
    assert twelvedata_symbol("EURUSD") == "EUR/USD"
    assert twelvedata_symbol("XAUUSD") == "XAU/USD"
    assert twelvedata_symbol("gbpjpy") == "GBP/JPY"


def test_validate_sim_bar_rejects_bad_ohlc():
    assert validate_sim_bar(SimBar("EURUSD", "2026-01-01", 1, 0.9, 1.1, 1.0, "biquote", None)) == ["range_inconsistent"]


def test_validate_sim_bar_accepts_good_bar():
    bar = SimBar("EURUSD", "2021-01-04", 1.10, 1.11, 1.09, 1.105,
                 "biquote", None)
    assert validate_sim_bar(bar, previous_close=1.10) == []


def test_clean_excludes_today_bar_from_both_providers():
    from app.services import sim_market_data as smd

    today = today_utc()
    bq = [SimBar("EURUSD", today, 1.1, 1.11, 1.09, 1.10, "biquote", None),
          SimBar("EURUSD", "2021-01-04", 1.1, 1.11, 1.09, 1.10,
                 "biquote", None)]
    td = [SimBar("EURUSD", today, 1.1, 1.11, 1.09, 1.10, "twelvedata", None),
          SimBar("EURUSD", "2021-01-03", 1.1, 1.11, 1.09, 1.10,
                 "twelvedata", None)]
    clean_bq, _ = smd._clean_provider_bars(bq, "2021-01-01", today)
    clean_td, _ = smd._clean_provider_bars(td, "2021-01-01", today)
    assert all(b.bar_date < today for b in clean_bq)
    assert all(b.bar_date < today for b in clean_td)
    assert today not in [b.bar_date for b in clean_bq]
    assert today not in [b.bar_date for b in clean_td]


@pytest.mark.asyncio
async def test_twelvedata_fetch_drops_today_bar(monkeypatch):
    import app.services.sim_market_data as smd

    monkeypatch.setattr(
        smd, "get_settings",
        lambda: SimpleNamespace(twelvedata_api_key="TESTKEY"),
    )
    today = today_utc()
    values = [
        {"datetime": f"{today} 00:00:00", "open": "1.10",
         "high": "1.11", "low": "1.09", "close": "1.105"},
        {"datetime": "2021-01-04 00:00:00", "open": "1.10",
         "high": "1.11", "low": "1.09", "close": "1.105"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "values": values})

    async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)) as client:
        bars = await fetch_twelvedata_history("EURUSD", years=5,
                                              client=client)
    assert all(b.bar_date < today for b in bars)
    assert [b.bar_date for b in bars] == ["2021-01-04"]
