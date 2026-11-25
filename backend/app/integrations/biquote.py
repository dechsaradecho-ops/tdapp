"""Biquote REST adapter for XAUUSD (phase one: REST only, no WebSocket).

Trusted gold order-data chain: Biquote -> Twelve Data XAU/USD.
Base URL: https://biquote.io (no API key or signup).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://biquote.io"
SYMBOL = "XAUUSD"
MAX_QUOTE_AGE_SECONDS = 300


class BiquoteError(Exception):
    """Raised for HTTP, schema, freshness, and market-state failures."""


@dataclass(frozen=True)
class BiquoteTick:
    symbol: str
    bid: float
    ask: float
    mid: float
    spread: float
    timestamp: datetime
    source: str
    exchange: str
    market_state: str
    stale: bool
    quote_age_seconds: int


@dataclass(frozen=True)
class BiquoteBar:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    is_open: bool


def _parse_time(value: object) -> datetime:
    try:
        text = str(value or "")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError) as exc:
        raise BiquoteError("biquote: invalid timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def fetch_tick(
    *,
    allow_stale: bool = False,
    client: httpx.AsyncClient | None = None,
) -> BiquoteTick:
    """Fetch the live XAUUSD quote. Rejects stale/closed quotes by default."""
    url = f"{BASE_URL}/api/{SYMBOL}"

    async def _get(c: httpx.AsyncClient) -> httpx.Response:
        return await c.get(url, params={"allowStale": "false"}, timeout=10.0)

    try:
        if client is not None:
            resp = await _get(client)
        else:
            async with httpx.AsyncClient() as own:
                resp = await _get(own)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        log.warning("biquote tick request failed: %s", type(exc).__name__)
        raise BiquoteError("biquote: tick request failed") from exc
    except ValueError as exc:
        log.warning("biquote tick response not JSON")
        raise BiquoteError("biquote: invalid tick payload") from exc

    try:
        bid = float(payload["bid"])
        ask = float(payload["ask"])
        mid = float(payload.get("mid", (bid + ask) / 2.0))
        spread = float(payload.get("spread", ask - bid))
        timestamp = _parse_time(payload["timestamp"])
        source = str(payload.get("source") or "")
        exchange = str(payload.get("exchange") or "")
        market_state = str(payload.get("marketState") or "")
        stale = bool(payload.get("stale", False))
        age = int(payload.get("quoteAgeSeconds", 0))
        symbol = str(payload.get("symbol") or SYMBOL)
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("biquote tick schema invalid")
        raise BiquoteError("biquote: invalid tick payload") from exc

    if not allow_stale:
        if market_state != "open":
            log.warning("biquote market not open (state=%s)", market_state)
            raise BiquoteError("biquote: market not open")
        if stale or age > MAX_QUOTE_AGE_SECONDS:
            log.warning("biquote quote stale (stale=%s age=%s)", stale, age)
            raise BiquoteError("biquote: stale quote")

    return BiquoteTick(
        symbol=symbol,
        bid=bid,
        ask=ask,
        mid=mid,
        spread=spread,
        timestamp=timestamp,
        source=source,
        exchange=exchange,
        market_state=market_state,
        stale=stale,
        quote_age_seconds=age,
    )


async def fetch_daily_bars(
    *,
    limit: int = 500,
    client: httpx.AsyncClient | None = None,
) -> list[BiquoteBar]:
    """Fetch daily OHLC bars, oldest-first (preserves is_open flag)."""
    url = f"{BASE_URL}/api/{SYMBOL}/ohlc"

    async def _get(c: httpx.AsyncClient) -> httpx.Response:
        return await c.get(
            url, params={"interval": "1d", "limit": str(limit)}, timeout=15.0
        )

    try:
        if client is not None:
            resp = await _get(client)
        else:
            async with httpx.AsyncClient() as own:
                resp = await _get(own)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        log.warning("biquote ohlc request failed: %s", type(exc).__name__)
        raise BiquoteError("biquote: ohlc request failed") from exc
    except ValueError as exc:
        log.warning("biquote ohlc response not JSON")
        raise BiquoteError("biquote: invalid ohlc payload") from exc

    try:
        raw_bars = payload["bars"]
        if not isinstance(raw_bars, list):
            raise TypeError("bars is not a list")
        bars: list[BiquoteBar] = []
        for row in raw_bars:
            tick_vol = row.get("tickVolume", row.get("volume", 0))
            bars.append(
                BiquoteBar(
                    open_time=_parse_time(row["openTime"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(tick_vol or 0),
                    is_open=bool(row.get("isOpen", False)),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("biquote ohlc schema invalid")
        raise BiquoteError("biquote: invalid ohlc payload") from exc

    bars.sort(key=lambda b: b.open_time)
    return bars
