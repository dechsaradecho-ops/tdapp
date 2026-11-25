"""Unit tests for app/integrations/biquote.py — all offline (MockTransport).

Run from backend/: pytest tests/test_biquote.py -v
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.biquote import (
    BiquoteBar,
    BiquoteError,
    BiquoteTick,
    fetch_daily_bars,
    fetch_tick,
)


def _tick_payload(*, stale: bool, market_state: str, age: int) -> dict:
    return {
        "symbol": "XAUUSD",
        "bid": 3517.981,
        "ask": 3518.136,
        "mid": 3518.0585,
        "spread": 0.155,
        "timestamp": "2026-11-25T08:08:20Z",
        "source": "MetaTrader 5 (Broker 1)",
        "exchange": "OTC",
        "marketState": market_state,
        "stale": stale,
        "quoteAgeSeconds": age,
    }


def _bars_payload() -> dict:
    return {
        "symbol": "XAUUSD",
        "interval": "1d",
        "bars": [
            {
                "openTime": "2026-11-25T00:00:00Z",
                "open": 3520.0,
                "high": 3530.0,
                "low": 3510.0,
                "close": 3525.0,
                "volume": 100,
                "tickVolume": 100,
                "isOpen": True,
            },
            {
                "openTime": "2026-11-24T00:00:00Z",
                "open": 3510.0,
                "high": 3522.0,
                "low": 3505.0,
                "close": 3520.0,
                "volume": 90,
                "tickVolume": 90,
                "isOpen": False,
            },
            {
                "openTime": "2026-11-23T00:00:00Z",
                "open": 3500.0,
                "high": 3515.0,
                "low": 3495.0,
                "close": 3510.0,
                "volume": 80,
                "tickVolume": 80,
                "isOpen": False,
            },
        ],
    }


def _mock_client(payload: dict, status: int = 200) -> httpx.AsyncClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


async def _run_fetch_tick(*, stale: bool, market_state: str, age: int,
                          allow_stale: bool = False) -> BiquoteTick:
    async with _mock_client(_tick_payload(stale=stale, market_state=market_state, age=age)) as client:
        return await fetch_tick(allow_stale=allow_stale, client=client)


async def _run_fetch_bars() -> list[BiquoteBar]:
    async with _mock_client(_bars_payload()) as client:
        return await fetch_daily_bars(client=client)


def run_fetch_tick_fixture(stale: bool, market_state: str, age: int) -> BiquoteTick:
    import asyncio

    return asyncio.run(
        _run_fetch_tick(stale=stale, market_state=market_state, age=age)
    )


def run_fetch_bars_fixture() -> list[BiquoteBar]:
    import asyncio

    return asyncio.run(_run_fetch_bars())


def test_fetch_tick_parses_live_quote():
    tick = run_fetch_tick_fixture(stale=False, market_state="open", age=0)
    assert tick.mid == 3518.0585
    assert tick.spread == 0.155
    assert tick.source == "MetaTrader 5 (Broker 1)"
    assert tick.quote_age_seconds == 0


def test_fetch_tick_rejects_stale_or_closed_quote():
    with pytest.raises(BiquoteError):
        run_fetch_tick_fixture(stale=True, market_state="open", age=600)
    with pytest.raises(BiquoteError):
        run_fetch_tick_fixture(stale=False, market_state="closed", age=0)


def test_fetch_daily_bars_normalizes_order_and_open_flag():
    bars = run_fetch_bars_fixture()
    assert bars[0].open_time < bars[-1].open_time
    assert bars[-1].is_open is True


# ---- Additional edge-case coverage ----------------------------------------

@pytest.mark.asyncio
async def test_fetch_tick_rejects_stale_age_without_flag():
    async with _mock_client(_tick_payload(stale=False, market_state="open", age=600)) as client:
        with pytest.raises(BiquoteError):
            await fetch_tick(client=client)


@pytest.mark.asyncio
async def test_fetch_tick_allows_stale_when_flag_set():
    async with _mock_client(_tick_payload(stale=True, market_state="open", age=600)) as client:
        tick = await fetch_tick(allow_stale=True, client=client)
    assert tick.stale is True
    assert tick.quote_age_seconds == 600


@pytest.mark.asyncio
async def test_fetch_tick_http_error_raises():
    async with _mock_client({}, status=503) as client:
        with pytest.raises(BiquoteError):
            await fetch_tick(client=client)


@pytest.mark.asyncio
async def test_fetch_bars_http_error_raises():
    async with _mock_client({}, status=500) as client:
        with pytest.raises(BiquoteError):
            await fetch_daily_bars(client=client)
