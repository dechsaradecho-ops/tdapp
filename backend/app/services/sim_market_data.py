"""Sim price-data provider adapters (V2, no persistence).

Biquote is authoritative for the most recent 500 daily bars per asset;
Twelve Data fills older history (default five years). Every bar carries
``source``; invalid bars are quarantined by the caller, never averaged
or mixed for the same date.

Twelve Data free-tier limits (8 req/min, 800/day) are honoured by the
later sync service — these adapters do one request per call and expose
no batching, so the throttle point stays in one place.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.core.config import get_settings
from app.integrations.biquote import fetch_daily_bars
from app.services.price_history import SPIKE_PCT, today_utc

log = logging.getLogger(__name__)

TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
TWELVEDATA_OUTPUTSIZE = "5000"


class SimMarketDataError(Exception):
    """Raised for missing key, HTTP, schema, or provider-error failures."""


@dataclass(frozen=True)
class SimBar:
    asset: str
    bar_date: str  # ISO YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    source: str  # "biquote" | "twelvedata"
    source_timestamp: str | None = None


def twelvedata_symbol(asset: str) -> str:
    """Map a compact asset code to a Twelve Data symbol.

    ``EURUSD`` -> ``EUR/USD``, ``XAUUSD`` -> ``XAU/USD``. Values already
    containing ``/`` pass through upper-cased.
    """
    text = str(asset or "").strip().upper().replace(" ", "")
    if "/" in text:
        return text
    if len(text) == 6 and text.isalpha():
        return f"{text[:3]}/{text[3:]}"
    return text


async def fetch_biquote_recent(
    asset: str, client: httpx.AsyncClient
) -> list[SimBar]:
    """Fetch the most recent 500 daily bars from Biquote, dropping the open bar.

    The newest Biquote bar is still forming intraday (``is_open``) and must
    not seed frozen history. Returns oldest-first SimBars with
    ``source="biquote"``.
    """
    symbol = str(asset or "").strip().upper()
    raw = await fetch_daily_bars(symbol, limit=500, client=client)
    bars: list[SimBar] = []
    for row in raw:
        if row.is_open:
            continue
        bar_date = row.open_time.astimezone(timezone.utc).date().isoformat()
        bars.append(SimBar(
            asset=symbol,
            bar_date=bar_date,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            source="biquote",
            source_timestamp=row.open_time.astimezone(timezone.utc).isoformat(),
        ))
    bars.sort(key=lambda b: b.bar_date)
    return bars


async def fetch_twelvedata_history(
    asset: str, *, years: int, client: httpx.AsyncClient
) -> list[SimBar]:
    """Fetch older daily history from Twelve Data ``/time_series``.

    One request with ``interval=1day`` and ``outputsize=5000`` (covers the
    default five-year span for FX trading days). Twelve Data returns
    newest-first; bars are reversed to oldest-first with UTC date
    normalization. Rows with non-finite/non-positive OHLC are skipped.
    """
    _ = years  # span is covered by outputsize=5000; kept for the sync API.
    symbol = str(asset or "").strip().upper()
    api_key = get_settings().twelvedata_api_key
    if not api_key:
        raise SimMarketDataError(
            f"{symbol}: TWELVEDATA_API_KEY not set (free key: twelvedata.com)")
    params = {"symbol": twelvedata_symbol(symbol), "interval": "1day",
              "outputsize": TWELVEDATA_OUTPUTSIZE, "apikey": api_key}
    try:
        resp = await client.get(TWELVEDATA_URL, params=params, timeout=15.0)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SimMarketDataError(
            f"{symbol}: twelvedata request failed ({exc})") from exc
    if not isinstance(payload, dict):
        raise SimMarketDataError(f"{symbol}: twelvedata invalid payload")
    if str(payload.get("status", "")) == "error":
        raise SimMarketDataError(
            f"{symbol}: twelvedata error {payload.get('code')}: "
            f"{payload.get('message')}")
    values = payload.get("values") or []
    if not isinstance(values, list):
        raise SimMarketDataError(f"{symbol}: twelvedata invalid payload")

    bars: list[SimBar] = []
    for row in reversed(values):  # TD returns newest-first -> flip
        if not isinstance(row, dict):
            continue
        try:
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if any(not math.isfinite(v) or v <= 0 for v in (o, h, l, c)):
            continue
        try:
            bar_date = datetime.fromisoformat(
                str(row.get("datetime") or "")[:10]).date().isoformat()
        except ValueError:
            continue
        bars.append(SimBar(
            asset=symbol,
            bar_date=bar_date,
            open=o,
            high=h,
            low=l,
            close=c,
            source="twelvedata",
            source_timestamp=str(row.get("datetime") or "") or None,
        ))
    bars.sort(key=lambda b: b.bar_date)
    return bars


@dataclass(frozen=True)
class DatasetBuildReport:
    version: int
    assets: list[str] = field(default_factory=list)
    bars: int = 0
    per_asset: dict[str, int] = field(default_factory=dict)
    seam_discrepancies: list[dict] = field(default_factory=list)
    invalid: list[dict] = field(default_factory=list)
    complete: bool = False
    data_hash: str = ""


def _ohlc(bar: SimBar) -> dict:
    return {"open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close}


def merge_history(
    asset: str, biquote: list[SimBar], twelvedata: list[SimBar]
) -> tuple[list[SimBar], list[dict]]:
    """Merge Twelve Data history with Biquote recents, oldest-first.

    Biquote wins every overlapping date; each overlap is recorded with
    both OHLC sets and never averaged. Output is deterministic.
    """
    by_date: dict[str, SimBar] = {}
    td_by_date: dict[str, SimBar] = {}
    for bar in twelvedata or []:
        by_date.setdefault(bar.bar_date, bar)
        td_by_date.setdefault(bar.bar_date, bar)
    discrepancies: list[dict] = []
    for bar in biquote or []:
        old = by_date.get(bar.bar_date)
        if old is not None:
            td_bar = td_by_date.get(bar.bar_date, old)
            discrepancies.append({
                "asset": asset,
                "bar_date": bar.bar_date,
                "biquote": _ohlc(bar),
                "twelvedata": _ohlc(td_bar),
                "selected_source": "biquote",
            })
        by_date[bar.bar_date] = bar
    merged = [by_date[k] for k in sorted(by_date)]
    discrepancies.sort(key=lambda d: d["bar_date"])
    return merged, discrepancies


def hash_bars(bars: list[SimBar]) -> str:
    """Canonical SHA-256 over sorted (asset, bar_date, OHLC, source) rows."""
    rows = sorted(bars or [], key=lambda b: (b.asset, b.bar_date))
    digest = hashlib.sha256()
    for b in rows:
        digest.update(
            f"{b.asset}|{b.bar_date}|{b.open!r}|{b.high!r}|{b.low!r}"
            f"|{b.close!r}|{b.source}\n".encode("utf-8"))
    return digest.hexdigest()


def validate_sim_bar(
    bar: SimBar, previous_close: float | None = None
) -> list[str]:
    """Check ONE SimBar. Returns reason codes; empty = usable.

    Mirrors ``price_history.validate_bar`` rules with SimBar-specific
    reason codes (single ``range_inconsistent`` for any OHLC geometry
    violation). Never raises.
    """
    try:
        return _validate(bar, previous_close)
    except Exception as exc:  # noqa: BLE001 - quarantine, don't crash
        log.debug("validate sim bar %s failed closed: %s", bar, exc)
        return ["validator_error"]


def _validate(bar: SimBar, previous_close: float | None) -> list[str]:
    errs: list[str] = []
    bar_date = bar.bar_date
    if not bar_date or not isinstance(bar_date, str):
        return ["no_date"]
    try:
        datetime.fromisoformat(bar_date).date()
    except ValueError:
        return ["bad_date"]
    if bar_date > today_utc():
        errs.append("future_bar")
    vals: dict[str, float] = {}
    for name in ("open", "high", "low", "close"):
        try:
            f = float(getattr(bar, name))
        except (TypeError, ValueError):
            errs.append(f"bad_{name}")
            continue
        if not math.isfinite(f) or f <= 0:
            errs.append(f"bad_{name}")
            continue
        vals[name] = f
    if errs:
        return errs
    o_, h_, l_, c_ = vals["open"], vals["high"], vals["low"], vals["close"]
    eps = max(o_, h_, l_, c_) * 1e-9
    if h_ < l_ - eps or h_ < max(o_, c_) - eps or l_ > min(o_, c_) + eps:
        errs.append("range_inconsistent")
    if previous_close:
        try:
            pc = float(previous_close)
            if pc > 0 and abs(c_ - pc) / pc * 100.0 > SPIKE_PCT:
                errs.append("spike_suspect")
        except (TypeError, ValueError):
            pass
    return errs
