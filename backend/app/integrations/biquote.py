"""Biquote REST adapter for all symbols (phase one: REST only, no WebSocket).

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


async def _get_json(
    url: str,
    params: dict,
    timeout: float,
    client: httpx.AsyncClient | None,
) -> object:
    async def _get(c: httpx.AsyncClient) -> httpx.Response:
        return await c.get(url, params=params, timeout=timeout)

    try:
        if client is not None:
            resp = await _get(client)
        else:
            async with httpx.AsyncClient() as own:
                resp = await _get(own)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        log.warning("biquote request failed: %s %s", url, type(exc).__name__)
        raise BiquoteError("biquote: request failed") from exc
    except ValueError as exc:
        log.warning("biquote response not JSON: %s", url)
        raise BiquoteError("biquote: invalid payload") from exc


def _parse_tick_payload(payload: dict, default_symbol: str) -> BiquoteTick:
    """Parse one tick object. Schema only — no freshness rejection."""
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
        symbol = str(payload.get("symbol") or default_symbol)
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("biquote tick schema invalid")
        raise BiquoteError("biquote: invalid tick payload") from exc
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


def _check_fresh(tick: BiquoteTick) -> None:
    if tick.market_state != "open":
        log.warning("biquote market not open (state=%s)", tick.market_state)
        raise BiquoteError("biquote: market not open")
    if tick.stale or tick.quote_age_seconds > MAX_QUOTE_AGE_SECONDS:
        log.warning(
            "biquote quote stale (stale=%s age=%s)",
            tick.stale,
            tick.quote_age_seconds,
        )
        raise BiquoteError("biquote: stale quote")


async def fetch_tick(
    symbol: str = SYMBOL,
    *,
    allow_stale: bool = False,
    client: httpx.AsyncClient | None = None,
) -> BiquoteTick:
    """Fetch the live quote for ``symbol``. Rejects stale/closed quotes by default."""
    url = f"{BASE_URL}/api/{symbol}"
    payload = await _get_json(url, {"allowStale": "false"}, 10.0, client)
    if not isinstance(payload, dict):
        log.warning("biquote tick schema invalid")
        raise BiquoteError("biquote: invalid tick payload")
    tick = _parse_tick_payload(payload, symbol)
    if not allow_stale:
        _check_fresh(tick)
    return tick


async def fetch_daily_bars(
    symbol: str = SYMBOL,
    *,
    limit: int = 500,
    client: httpx.AsyncClient | None = None,
) -> list[BiquoteBar]:
    """Fetch daily OHLC bars for ``symbol``, oldest-first (preserves is_open flag)."""
    url = f"{BASE_URL}/api/{symbol}/ohlc"
    payload = await _get_json(
        url, {"interval": "1d", "limit": str(limit)}, 15.0, client
    )

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


async def fetch_latest(
    symbols: list[str],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, BiquoteTick]:
    """Fetch live quotes for many symbols with ONE ``GET /api/latest`` call.

    The response is an object keyed by symbol. Entries that fail schema
    parsing are skipped (logged) so one bad symbol never fails the rest.
    Freshness flags (``market_state``/``stale``/age) are preserved on the
    returned ticks — callers decide precedence (e.g. fresh-Yahoo-over-stale-
    Biquote), so no freshness rejection happens here.
    """
    if not symbols:
        return {}
    url = f"{BASE_URL}/api/latest"
    payload = await _get_json(url, {"symbols": list(symbols)}, 10.0, client)
    if not isinstance(payload, dict):
        log.warning("biquote latest schema invalid")
        raise BiquoteError("biquote: invalid latest payload")
    ticks: dict[str, BiquoteTick] = {}
    for symbol in symbols:
        entry = payload.get(symbol)
        if not isinstance(entry, dict):
            log.warning("biquote latest missing entry for %s", symbol)
            continue
        try:
            ticks[symbol] = _parse_tick_payload(entry, symbol)
        except BiquoteError:
            continue
    return ticks
