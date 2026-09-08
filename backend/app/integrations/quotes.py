"""Live market data fetcher.

Feed chain (2026-09-07 — Yahoo เป็นหลัก):
  1) Yahoo chart API — REAL intraday OHLC candles สำหรับทุกคู่ใน
     SUPPORTED_ASSETS (FX ผ่าน `XXXYYY=X` ทุกคู่, ทองผ่าน COMEX `GC=F`)
  2) Frankfurter (ECB) — FX เท่านั้น, เฉพาะราคาปิดรายวัน (fallback เมื่อ
     Yahoo ล่ม); ฟรี ไม่ต้องมี API key, OHLC สังเคราะห์จาก close ต่อเนื่อง
  2b) Twelve Data — ทองคำ (XAUUSD) เฉพาะเมื่อ Yahoo ล่ม, ต้องมี API key
  3) Spot: Yahoo → exchangerate-api.com (6 rotating keys, คู่ ECB ทั้งหมด)

Degrades gracefully: on network failure it raises QuotesUnavailable and the
market scanner falls back to the random-walk demo feed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from app.core.config import get_settings
from app.services import quote_log

log = logging.getLogger(__name__)

# Feed routing per asset — SECONDARY feeds only. Yahoo candles are the
# primary source for every supported asset (see fetch_candles); the mapping
# below is the fallback chain used when the Yahoo candle request fails.
FEED_FRANKFURTER = "frankfurter"   # FX daily closes (no key)
FEED_TWELVEDATA = "twelvedata"     # Gold OHLC (free key required)
FEED_YAHOO = "yahoo"               # primary: real intraday OHLC, no key

ASSET_FEEDS: dict[str, str] = {
    "EURUSD": FEED_FRANKFURTER,
    "GBPUSD": FEED_FRANKFURTER,
    "USDJPY": FEED_FRANKFURTER,
    "AUDUSD": FEED_FRANKFURTER,
    "XAUUSD": FEED_TWELVEDATA,
}

# ---- Supported trading universe (Settings page list) ---------------------
# Every pair here is covered by the PRIMARY feed (Yahoo: any FX spot symbol
# `XXXYYY=X`) AND by the fallbacks (Frankfurter/exchangerate support any
# ECB-reference pair; gold stays on TwelveData/GC=F). Pairs listed = pairs
# the app can actually price — nothing outside this registry is selectable
# in the Settings page.
DEFAULT_ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"]

SUPPORTED_ASSETS: list[str] = [
    # Majors
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF",
    # EUR crosses
    "EURGBP", "EURJPY", "EURAUD", "EURNZD", "EURCAD", "EURCHF",
    # GBP crosses
    "GBPJPY", "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF",
    # AUD / NZD crosses
    "AUDJPY", "AUDNZD", "AUDCAD", "AUDCHF", "NZDJPY", "NZDCAD",
    # CAD / CHF crosses
    "CADJPY", "CADCHF", "CHFJPY",
    # Metals (Yahoo: COMEX future GC=F, TwelveData: XAU/USD)
    "XAUUSD",
]

# Currencies covered by the fallback rate APIs (ECB reference set — the
# Frankfurter + exchangerate-api payload universe).
_ECB_CURRENCIES = {
    "AUD", "BGN", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP",
    "HKD", "HUF", "IDR", "ILS", "INR", "ISK", "JPY", "KRW", "MXN", "MYR",
    "NOK", "NZD", "PHP", "PLN", "RON", "SEK", "SGD", "THB", "TRY", "USD",
    "ZAR",
}

# asset → (base, quote) for Frankfurter
FX_PAIRS: dict[str, tuple[str, str]] = {
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "AUDUSD": ("AUD", "USD"),
}


def fx_parts(asset: str) -> tuple[str, str] | None:
    """6-char FX symbol → (base, quote); None for non-FX (XAUUSD) assets.

    Generic derivation keeps every SUPPORTED_ASSETS FX pair priceable by
    the fallback rate feeds without per-pair tables. Metals (XAUUSD) are
    excluded — they have no rate-API mapping and must stay on their own
    feeds (Yahoo GC=F / TwelveData).
    """
    a = str(asset).upper()
    if len(a) != 6 or not a.isalpha() or a in ("XAUUSD", "XAGUSD"):
        return None
    return a[:3], a[3:]


def is_supported_asset(asset: str) -> bool:
    """True when the pair is in the priceable universe (Settings whitelist)."""
    return str(asset).upper() in SUPPORTED_ASSETS

FRANKFURTER_URL = "https://api.frankfurter.dev/v1"  # .app domain 301-redirects มาที่นี่
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
GOLD_SYMBOL = "XAU/USD"

# --- Spot feed (Yahoo chart API first, exchangerate-api.com fallback) -----
# Frankfurter only publishes ONE close per business day (ECB), so intraday
# positions opened at today's close look "pinned" until tomorrow.
# Priority: Yahoo chart API (real intraday FX spots + gold via the COMEX
# future GC=F) → v6.exchangerate-api.com (6 rotating keys, FX pairs only).
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
YAHOO_SYMBOLS: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "XAUUSD": "GC=F",  # COMEX gold future ≈ spot (no XAUUSD symbol on Yahoo)
}


def _yahoo_symbol(asset: str) -> str | None:
    """asset → Yahoo chart symbol (explicit map first, generic FX after).

    Yahoo serves ANY FX spot pair as `XXXYYY=X`, so every 6-letter FX pair
    in SUPPORTED_ASSETS works without a per-pair table. Gold maps to the
    COMEX future GC=F. Non-FX / unknown assets return None.
    """
    a = str(asset).upper()
    if a in YAHOO_SYMBOLS:
        return YAHOO_SYMBOLS[a]
    if fx_parts(a) and a in SUPPORTED_ASSETS:
        return f"{a}=X"
    return None
SPOT_TTL = 30.0  # seconds — monitor polls every 10s; 30s cache keeps feeds tiny
_spot_cache: dict[str, tuple[float, float]] = {}  # asset → (monotonic_ts, price)

# exchangerate-api.com state — keys rotate when one exhausts its quota (429)
EXCHANGERATE_URL = "https://v6.exchangerate-api.com/v6"
_exchange_key_idx = 0


def _exchange_pair(asset: str) -> tuple[str, str] | None:
    """asset → (base, quote) for exchangerate-api; None for unsupported (XAUUSD)."""
    if asset in FX_PAIRS:
        return FX_PAIRS[asset]
    parts = fx_parts(asset)
    if parts and all(c in _ECB_CURRENCIES for c in parts):
        return parts
    return None


async def _fetch_spot_exchangerate(asset: str) -> tuple[float, str]:
    """Spot price via exchangerate-api.com, rotating through all keys.

    /latest/{base} returns conversion rates for every quote currency, so one
    request per key covers all four FX pairs. Returns (price, "") on success
    or (0.0, reason) when every key fails.
    """
    global _exchange_key_idx
    pair = _exchange_pair(asset)
    if pair is None:
        return 0.0, f"{asset}: no exchangerate mapping"

    base, quote = pair
    keys = get_settings().exchangerate_key_list
    if not keys:
        return 0.0, "no EXCHANGERATE_API_KEYS configured"

    last_err = "no keys tried"
    for _ in range(len(keys)):
        key = keys[_exchange_key_idx % len(keys)]
        _exchange_key_idx += 1
        url = f"{EXCHANGERATE_URL}/{key}/latest/{base}"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(url, timeout=10.0)
                dur = int((time.monotonic() - t0) * 1000)
                if resp.status_code == 429 or resp.status_code >= 400:
                    last_err = f"exchangerate key #{_exchange_key_idx % len(keys)}: HTTP {resp.status_code}"
                    quote_log.log_call(
                        asset=asset, category=quote_log.category_for(asset),
                        provider="exchangerate", url=url, status="error",
                        http_status=resp.status_code,
                        error=f"HTTP {resp.status_code}",
                        duration_ms=dur,
                        api_key_hint=quote_log.mask_key(key))
                    continue  # rotate to the next key
                payload = resp.json()
                rate = ((payload.get("conversion_rates") or {}).get(quote))
                if not rate:
                    last_err = f"{asset}: no {quote} rate in exchangerate payload"
                    quote_log.log_call(
                        asset=asset, category=quote_log.category_for(asset),
                        provider="exchangerate", url=url, status="error",
                        http_status=resp.status_code,
                        error=f"no {quote} rate in payload",
                        duration_ms=dur,
                        api_key_hint=quote_log.mask_key(key))
                    continue
                quote_log.log_call(
                    asset=asset, category=quote_log.category_for(asset),
                    provider="exchangerate", url=url, status="success",
                    http_status=resp.status_code, price=float(rate),
                    duration_ms=dur,
                    api_key_hint=quote_log.mask_key(key))
                return float(rate), ""
        except (httpx.HTTPError, ValueError) as exc:
            dur = int((time.monotonic() - t0) * 1000)
            last_err = f"exchangerate request failed ({exc})"
            quote_log.log_call(
                asset=asset, category=quote_log.category_for(asset),
                provider="exchangerate", url=url, status="error",
                error=str(exc), duration_ms=dur,
                api_key_hint=quote_log.mask_key(key))
            continue
    return 0.0, f"{asset}: {last_err}"


class QuotesUnavailable(Exception):
    """Raised when the live data feed fails — caller decides on fallback."""


@dataclass
class Candle:
    o: float
    h: float
    l: float
    c: float


# Yahoo /chart 1d candles: requested calendar span per `days` of bars.
def _yahoo_candle_range(days: int) -> str:
    if days <= 7:
        return "5d"
    if days <= 30:
        return "1mo"
    if days <= 90:
        return "3mo"
    if days <= 365:
        return "1y"
    return "2y"


async def _fetch_yahoo_candles(asset: str, client: httpx.AsyncClient,
                               days: int) -> list[Candle]:
    """PRIMARY candle feed — Yahoo chart API daily OHLC, oldest-first.

    Covers every SUPPORTED_ASSETS pair (FX via `XXXYYY=X`, gold via the
    COMEX future GC=F) with REAL high/low data — no key required. Null bars
    (holidays/half sessions) are skipped. Raises QuotesUnavailable on any
    HTTP/parse failure so the caller can fall through to Frankfurter/
    TwelveData.
    """
    sym = _yahoo_symbol(asset)
    if not sym:
        raise QuotesUnavailable(f"{asset}: no yahoo symbol mapping")
    url = f"{YAHOO_CHART_URL}/{sym}"
    t0 = time.monotonic()
    try:
        resp = await client.get(
            url, params={"interval": "1d", "range": _yahoo_candle_range(days)},
            headers={"User-Agent": YAHOO_UA}, timeout=15.0)
        dur = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        dur = int((time.monotonic() - t0) * 1000)
        quote_log.log_call(
            asset=asset, category=quote_log.category_for(asset),
            provider="yahoo", url=url, status="error",
            error=str(exc), duration_ms=dur)
        raise QuotesUnavailable(f"{asset}: yahoo candle request failed ({exc})") from exc

    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    quote_block = (((result or {}).get("indicators") or {}).get("quote") or [None])[0]
    if not result or not quote_block:
        err = ((payload.get("chart") or {}).get("error") or {}).get("description", "empty chart payload")
        quote_log.log_call(
            asset=asset, category=quote_log.category_for(asset),
            provider="yahoo", url=url, status="error",
            http_status=resp.status_code, error=err, duration_ms=dur)
        raise QuotesUnavailable(f"{asset}: yahoo chart payload unusable ({err})")

    candles: list[Candle] = []
    for o, h, l, c in zip(quote_block.get("open") or [],
                          quote_block.get("high") or [],
                          quote_block.get("low") or [],
                          quote_block.get("close") or []):
        if o is None or h is None or l is None or c is None:
            continue  # holiday / half-session bar
        candles.append(Candle(o=float(o), h=float(h), l=float(l), c=float(c)))

    quote_log.log_call(
        asset=asset, category=quote_log.category_for(asset),
        provider="yahoo", url=url, status="success",
        http_status=resp.status_code,
        price=candles[-1].c if candles else None,
        duration_ms=dur)
    return candles


async def _fetch_fx(asset: str, client: httpx.AsyncClient,
                    days: int) -> list[Candle]:
    """Frankfurter time series → daily-close candles (oldest-first).

    FALLBACK feed (Yahoo ล่มเท่านั้น) — ECB publishes one close per business
    day; weekends/holidays are absent from the payload entirely (no null-gap
    rows to filter).
    """
    parts = FX_PAIRS.get(asset) or fx_parts(asset)
    if not parts:
        raise QuotesUnavailable(f"{asset}: no FX pair mapping for frankfurter")
    base, quote = parts
    # ~1.6 calendar days per trading day covers weekends + ECB holidays,
    # +10 days slack so short months still satisfy the 30-bar minimum.
    start = (date.today() - timedelta(days=int(days * 1.6) + 10)).isoformat()
    end = date.today().isoformat()
    url = f"{FRANKFURTER_URL}/{start}..{end}"
    t0 = time.monotonic()
    try:
        resp = await client.get(url, params={"from": base, "to": quote},
                                timeout=15.0)
        dur = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        dur = int((time.monotonic() - t0) * 1000)
        quote_log.log_call(
            asset=asset, category=quote_log.category_for(asset),
            provider="frankfurter", url=url, status="error",
            error=str(exc), duration_ms=dur)
        raise QuotesUnavailable(f"{asset}: frankfurter request failed ({exc})") from exc

    rates = payload.get("rates") or {}
    if not rates:
        quote_log.log_call(
            asset=asset, category=quote_log.category_for(asset),
            provider="frankfurter", url=url, status="error",
            http_status=resp.status_code, error="empty rates payload",
            duration_ms=dur)
        raise QuotesUnavailable(f"{asset}: empty frankfurter rates")

    closes: list[float] = []
    for day in sorted(rates):  # ISO dates sort chronologically
        val = (rates[day] or {}).get(quote)
        if val is None:
            continue
        closes.append(float(val))

    quote_log.log_call(
        asset=asset, category=quote_log.category_for(asset),
        provider="frankfurter", url=url, status="success",
        http_status=resp.status_code,
        price=closes[-1] if closes else None,
        duration_ms=dur)

    closes: list[float] = []
    for day in sorted(rates):  # ISO dates sort chronologically
        val = (rates[day] or {}).get(quote)
        if val is None:
            continue
        closes.append(float(val))

    # Synthesize OHLC from consecutive closes: open = previous close,
    # high/low envelope the bar. Honest representation of close-only data
    # and keeps TR/DM math (hence ATR/ADX) meaningful.
    candles: list[Candle] = []
    prev: float | None = None
    for c in closes:
        o = prev if prev is not None else c
        candles.append(Candle(o=o, h=max(o, c), l=min(o, c), c=c))
        prev = c
    return candles


async def _fetch_gold(client: httpx.AsyncClient, days: int) -> list[Candle]:
    """Twelve Data /time_series → real XAU/USD daily OHLC (oldest-first)."""
    api_key = get_settings().twelvedata_api_key
    if not api_key:
        raise QuotesUnavailable("XAUUSD: TWELVEDATA_API_KEY not set (free key: twelvedata.com)")
    params = {"symbol": GOLD_SYMBOL, "interval": "1day",
              "outputsize": str(days), "apikey": api_key}
    t0 = time.monotonic()
    try:
        resp = await client.get(TWELVEDATA_URL, params=params, timeout=15.0)
        dur = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        dur = int((time.monotonic() - t0) * 1000)
        quote_log.log_call(
            asset="XAUUSD", category="gold", provider="twelvedata",
            url=quote_log.strip_key_from_url(TWELVEDATA_URL), status="error",
            error=str(exc), duration_ms=dur,
            api_key_hint=quote_log.mask_key(api_key))
        raise QuotesUnavailable(f"XAUUSD: twelvedata request failed ({exc})") from exc

    if str(payload.get("status", "")) == "error":
        quote_log.log_call(
            asset="XAUUSD", category="gold", provider="twelvedata",
            url=quote_log.strip_key_from_url(TWELVEDATA_URL), status="error",
            http_status=resp.status_code,
            error=f"twelvedata error {payload.get('code')}: {payload.get('message')}",
            duration_ms=dur, api_key_hint=quote_log.mask_key(api_key))
        raise QuotesUnavailable(
            f"XAUUSD: twelvedata error {payload.get('code')}: {payload.get('message')}")

    values = payload.get("values") or []
    candles: list[Candle] = []
    for row in reversed(values):  # TD returns newest-first → flip
        try:
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        candles.append(Candle(o=o, h=h, l=l, c=c))

    quote_log.log_call(
        asset="XAUUSD", category="gold", provider="twelvedata",
        url=quote_log.strip_key_from_url(TWELVEDATA_URL), status="success",
        http_status=resp.status_code,
        price=candles[-1].c if candles else None,
        duration_ms=dur, api_key_hint=quote_log.mask_key(api_key))
    return candles


async def fetch_candles(asset: str, client: httpx.AsyncClient,
                        days: int = 40) -> list[Candle]:
    """Fetch ~`days` daily candles for an asset. Raises QuotesUnavailable.

    Chain: Yahoo (primary, real OHLC) → mapped fallback feed
    (Frankfurter FX / TwelveData gold). Only assets inside SUPPORTED_ASSETS
    (or the explicit ASSET_FEEDS map) are priceable.
    """
    if asset not in SUPPORTED_ASSETS and asset not in ASSET_FEEDS:
        raise QuotesUnavailable(f"no feed mapping for {asset}")

    candles: list[Candle] = []
    try:
        candles = await _fetch_yahoo_candles(asset, client, days)
    except QuotesUnavailable as yahoo_exc:
        feed = ASSET_FEEDS.get(asset)
        if feed is None:
            raise yahoo_exc from None
        try:
            if feed == FEED_TWELVEDATA:
                candles = await _fetch_gold(client, days)
            else:
                candles = await _fetch_fx(asset, client, days)
        except QuotesUnavailable as fallback_exc:
            raise QuotesUnavailable(
                f"{asset}: yahoo failed ({yahoo_exc}) and fallback failed ({fallback_exc})") from fallback_exc

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


def _breakout_state(candles: list[Candle], lookback: int = 20,
                    retest_tol_pct: float = 0.15) -> tuple[float, float]:
    """Breakout-retest state over the last ``lookback`` bars (gold-specific).

    The level is the highest high of the 20 bars BEFORE the previous bar
    (window excludes both the breakout bar and the current bar).

    Returns (breakout_state, breakout_level):
      state 2 = breakout: the last close crosses ABOVE the prior high
                while the previous close was still below/at it.
      state 1 = retest: the last candle dipped into the level zone
                (low ≤ level + tol) and closed ABOVE the level — the classic
                "retest สำเร็จ" entry trigger.
      state 0 = no active breakout setup.
    breakout_level = the broken N-bar high (invalidation reference for the
    SL), 0.0 when no breakout.
    """
    if len(candles) < lookback + 2:
        return 0.0, 0.0
    window = candles[-(lookback + 2):-2]
    level = max(c.h for c in window)
    last = candles[-1]
    prev = candles[-2]
    if last.c > level and prev.c <= level:
        return 2.0, round(level, 5)
    tol = level * retest_tol_pct / 100.0
    if last.c > level and last.l <= level + tol:
        return 1.0, round(level, 5)
    return 0.0, 0.0


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

    # Breakout-retest context (gold-specific entry strategy D). Other assets
    # keep 0.0 — the scanner gate only consumes it for XAUUSD.
    breakout_state, breakout_level = _breakout_state(candles) \
        if asset.upper() == "XAUUSD" else (0.0, 0.0)

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
        "breakout_state": breakout_state,
        "breakout_level": breakout_level,
    }


async def fetch_snapshot(asset: str, client: httpx.AsyncClient,
                         news_sentiment: float = 0.0,
                         high_impact_event: bool = False) -> dict:
    """Convenience: candles → indicator dict. Raises QuotesUnavailable."""
    candles = await fetch_candles(asset, client)
    return snapshot_from_candles(asset, candles, news_sentiment, high_impact_event)


async def fetch_spot_prices(assets: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    """Intraday spot prices via Yahoo chart API (exchangerate fallback), cached ~30s.

    Returns (prices, failures) where failures maps asset → human-readable
    reason (timeout/HTTP/missing data). Prices that fail are simply absent
    from the dict — callers show a feed-status banner instead of guessing.
    Per-asset failures are isolated: one dead symbol never blocks the rest.
    """
    now = time.monotonic()
    out: dict[str, float] = {}
    failures: dict[str, str] = {}
    todo: list[str] = []
    for a in assets:
        hit = _spot_cache.get(a)
        if hit is not None and now - hit[0] < SPOT_TTL:
            out[a] = hit[1]
        else:
            todo.append(a)
    if todo:
        headers = {"User-Agent": YAHOO_UA}

        async def _yahoo_one(asset: str) -> tuple[str, float | None, str]:
            sym = _yahoo_symbol(asset)
            if not sym:
                return asset, None, f"no spot symbol mapping for {asset}"
            url = f"{YAHOO_CHART_URL}/{sym}"
            t0 = time.monotonic()
            try:
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    resp = await client.get(
                        url,
                        params={"interval": "1m", "range": "1d"},
                        headers=headers, timeout=10.0)
                    dur = int((time.monotonic() - t0) * 1000)
                    resp.raise_for_status()
                    meta = ((resp.json().get("chart") or {}).get("result")
                            or [{}])[0].get("meta", {})
                    price = meta.get("regularMarketPrice")
                    if not price:
                        quote_log.log_call(
                            asset=asset, category=quote_log.category_for(asset),
                            provider="yahoo", url=url, status="error",
                            http_status=resp.status_code,
                            error="no regularMarketPrice", duration_ms=dur)
                        return asset, None, f"{asset}: no regularMarketPrice"
                    quote_log.log_call(
                        asset=asset, category=quote_log.category_for(asset),
                        provider="yahoo", url=url, status="success",
                        http_status=resp.status_code, price=float(price),
                        duration_ms=dur)
                    return asset, float(price), ""
            except httpx.TimeoutException:
                quote_log.log_call(
                    asset=asset, category=quote_log.category_for(asset),
                    provider="yahoo", url=url, status="error",
                    error="spot feed timeout",
                    duration_ms=int((time.monotonic() - t0) * 1000))
                return asset, None, f"{asset}: spot feed timeout"
            except (httpx.HTTPError, ValueError, IndexError, KeyError) as exc:
                quote_log.log_call(
                    asset=asset, category=quote_log.category_for(asset),
                    provider="yahoo", url=url, status="error",
                    error=str(exc),
                    duration_ms=int((time.monotonic() - t0) * 1000))
                return asset, None, f"{asset}: spot feed error ({exc})"

        async def _one(asset: str) -> tuple[str, float | None, str]:
            # 1) Yahoo chart API first (real intraday spots; only XAUUSD source)
            asset_y, y_price, y_err = await _yahoo_one(asset)
            if y_price:
                return asset, y_price, ""
            # 2) exchangerate-api.com fallback (6 rotating keys — FX pairs only)
            price, err = await _fetch_spot_exchangerate(asset)
            if price:
                return asset, price, ""
            return asset, None, f"{y_err}; {err}"

        results = await asyncio.gather(*[_one(a) for a in todo])
        now = time.monotonic()
        for asset, price, err in results:
            if price is not None:
                _spot_cache[asset] = (now, price)
                out[asset] = price
            else:
                failures[asset] = err
                log.warning("spot feed: %s", err)
    quote_log.purge_old_logs(quote_log.get_db())
    return out, failures


_QUOTE_TTL = 60.0  # seconds — protects Twelve Data's 800 credits/day
# from 10s dashboard polling (8,640 req/day → 1,440/day cached).
# (2026-09-04: tried 30 min to cut Frankfurter calls — user prefers fresh
# snapshots every minute, reverted.)
_quote_cache: dict[str, tuple[float, dict]] = {}


async def fetch_all_snapshots(assets: list[str], concurrency: int = 5,
                              ttl: float | None = None) -> dict[str, dict]:
    """Fetch snapshots for all assets concurrently (in-memory cached).

    Cache TTL defaults to 60s: FX comes from Frankfurter (free) and gold
    from Twelve Data (800 credits/day on the free tier) — uncached polling
    every 10s from a single monitor page would burn the daily quota in ~2h.
    Returns {asset: snapshot_dict} for successes; failures are logged and
    simply missing from the result (caller can decide fallback).
    """
    now = time.monotonic()
    effective_ttl = _QUOTE_TTL if ttl is None else ttl
    out: dict[str, dict] = {}
    missing: list[str] = []
    for a in assets:
        hit = _quote_cache.get(a)
        if hit is not None and now - hit[0] < effective_ttl:
            out[a] = hit[1]
        else:
            missing.append(a)
    if not missing:
        return out

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

    pairs = await asyncio.gather(*[_one(a) for a in missing])
    fetched = {a: s for a, s in pairs if s is not None}
    now = time.monotonic()
    for a, s in fetched.items():
        _quote_cache[a] = (now, s)
    out.update(fetched)
    return out
