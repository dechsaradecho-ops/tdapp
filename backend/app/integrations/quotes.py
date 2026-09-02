"""Live market data fetcher — Yahoo Finance chart API (no API key required).

Converts OHLCV candles into the IndicatorSnapshot fields the Strategy Engine
expects (EMA, ADX, Supertrend direction, RSI, MACD histogram, ATR%, etc).

Degrades gracefully: on network failure it raises QuotesUnavailable and the
market scanner falls back to the random-walk demo feed.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# Asset → Yahoo Finance symbol (FX daily candles; XAUUSD = gold futures in USD)
YAHOO_SYMBOLS = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "XAUUSD": "GC=F",
}

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class QuotesUnavailable(Exception):
    """Raised when the live data feed fails — caller decides on fallback."""


@dataclass
class Candle:
    o: float
    h: float
    l: float
    c: float


async def fetch_candles(asset: str, client: httpx.AsyncClient,
                        days: int = 40) -> list[Candle]:
    """Fetch ~`days` daily candles for an asset. Raises QuotesUnavailable."""
    symbol = YAHOO_SYMBOLS.get(asset)
    if not symbol:
        raise QuotesUnavailable(f"no symbol mapping for {asset}")

    url = CHART_URL.format(symbol=symbol)
    params = {"interval": "1d", "range": f"{days}d", "includePrePost": "false"}
    try:
        resp = await client.get(url, params=params, headers={"User-Agent": UA},
                                timeout=15.0)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise QuotesUnavailable(f"{asset}: request failed ({exc})") from exc

    result = (payload.get("chart") or {}).get("result")
    if not result:
        raise QuotesUnavailable(f"{asset}: empty chart result")

    ts = result[0].get("timestamp") or []
    quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []

    candles: list[Candle] = []
    for i in range(min(len(ts), len(closes))):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if None in (o, h, l, c):
            continue
        candles.append(Candle(o=float(o), h=float(h), l=float(l), c=float(c)))

    if len(candles) < 30:  # need enough bars for EMA200-substitute + ADX
        raise QuotesUnavailable(f"{asset}: only {len(candles)} candles")

    return candles


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    emas = [values[0]]
    for v in values[1:]:
        emas.append(v * k + emas[-1] * (1 - k))
    return emas


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def _atr_pct(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].c
        tr = max(candles[i].h - candles[i].l,
                 abs(candles[i].h - prev_close),
                 abs(candles[i].l - prev_close))
        trs.append(tr)
    trs = trs[-period:]
    atr = sum(trs) / len(trs)
    price = candles[-1].c
    if price <= 0:
        return 0.0
    return atr / price * 100.0


def _adx(candles: list[Candle], period: int = 14) -> float:
    """Simplified Wilder ADX (trend strength 0-100)."""
    if len(candles) < 2 * period + 1:
        return 0.0
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, len(candles)):
        up = candles[i].h - candles[i - 1].h
        down = candles[i - 1].l - candles[i].l
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        prev_close = candles[i - 1].c
        trs.append(max(candles[i].h - candles[i].l,
                       abs(candles[i].h - prev_close),
                       abs(candles[i].l - prev_close)))

    def _wilder_smooth(vals: list[float]) -> list[float]:
        out = [sum(vals[:period])]
        for v in vals[period:]:
            out.append(out[-1] - out[-1] / period + v)
        return out

    atr = _wilder_smooth(trs)
    plus = _wilder_smooth(plus_dm)
    minus = _wilder_smooth(minus_dm)

    dxs: list[float] = []
    for i in range(len(atr)):
        if atr[i] == 0:
            continue
        pdi = 100.0 * plus[i] / atr[i]
        mdi = 100.0 * minus[i] / atr[i]
        denom = (pdi + mdi)
        if denom == 0:
            continue
        dxs.append(100.0 * abs(pdi - mdi) / denom)

    if len(dxs) < period:
        return 0.0
    return sum(dxs[-period:]) / period


def _supertrend_dir(candles: list[Candle], period: int = 10, mult: float = 3.0) -> int:
    """Coarse Supertrend direction: +1 up / -1 down (price vs trailing band)."""
    if len(candles) < period + 1:
        return 0
    atr_pct = _atr_pct(candles, period)
    price = candles[-1].c
    sma = sum(c.c for c in candles[-period:]) / period
    if sma <= 0 or price <= 0:
        return 0
    band = sma * (atr_pct / 100.0) * mult
    if price > sma + band:
        return 1
    if price < sma - band:
        return -1
    # inside the band: follow recent closes direction
    return 1 if price > sma else -1


def snapshot_from_candles(asset: str, candles: list[Candle],
                          news_sentiment: float = 0.0,
                          high_impact_event: bool = False) -> dict:
    """Compute all engine fields from OHLCV candles. Returns a dict."""
    closes = [c.c for c in candles]
    price = closes[-1]
    ema_fast_series = _ema(closes, 50)
    ema_slow_series = _ema(closes, 100)
    ema_fast = ema_fast_series[-1]
    ema_slow = ema_slow_series[-1]

    # 20-day change
    ref = closes[-21] if len(closes) >= 21 else closes[0]
    change_pct = (price - ref) / ref * 100.0 if ref else 0.0

    ema_fast_series = ema_fast_series[-len(candles):]
    macd_hist = ema_fast_series[-1] - ema_slow_series[-1] if len(ema_slow_series) else 0.0
    # scale MACD histogram relative to price so thresholds behave across assets
    macd_hist = macd_hist / price * 100.0

    atr_pct = _atr_pct(candles)
    # volatility index proxy: normalized ATR (percent-of-price based 0-100 scale)
    vol_index = min(atr_pct * 8.0, 100.0)

    return {
        "asset": asset,
        "price": round(price, 5),
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "adx": round(_adx(candles), 1),
        "supertrend_dir": _supertrend_dir(candles),
        "rsi": round(_rsi(closes), 1),
        "macd_hist": round(macd_hist, 2),
        "price_change_pct_20": round(change_pct, 2),
        "atr_pct": round(atr_pct, 2),
        "volatility_index": round(vol_index, 1),
        "news_sentiment": news_sentiment,
        "high_impact_event": high_impact_event,
    }


async def fetch_snapshot(asset: str, client: httpx.AsyncClient,
                         news_sentiment: float = 0.0,
                         high_impact_event: bool = False) -> dict:
    """Convenience: candles → indicator dict. Raises QuotesUnavailable."""
    candles = await fetch_candles(asset, client)
    return snapshot_from_candles(asset, candles, news_sentiment, high_impact_event)


async def fetch_all_snapshots(assets: list[str], concurrency: int = 5) -> dict[str, dict]:
    """Fetch snapshots for all assets concurrently.

    Returns {asset: snapshot_dict} for successes; failures are logged and
    simply missing from the result (caller can decide fallback).
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(asset: str) -> tuple[str, dict | None]:
        async with sem:
            async with httpx.AsyncClient() as client:
                try:
                    return asset, await fetch_snapshot(asset, client)
                except QuotesUnavailable as exc:
                    log.warning("Live quote unavailable for %s: %s", asset, exc)
                    return asset, None
                except Exception as exc:  # unexpected — don't kill the scan
                    log.exception("Unexpected quote error for %s", asset)
                    return asset, None

    pairs = await asyncio.gather(*[_one(a) for a in assets])
    return {a: s for a, s in pairs if s is not None}
