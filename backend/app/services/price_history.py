"""Daily price history in Postgres — fetch once, validate, reuse.

WHY (owner 2026-10-06)
----------------------
Every simulation run re-fetched ~2 years of daily candles and threw them
away. This module stores one row per (asset, bar_date) and each sync fetches
only what is missing. Two rules keep the stored history trustworthy:

1. IMMUTABLE PAST. Only the latest stored bar may be refreshed (it can still
   be forming intraday). Older bars are never overwritten — a re-run on the
   same dates must see the same prices. A feed that disagrees with a frozen
   bar is reported as a conflict and NOT written.
2. VALIDATE BEFORE INSERT. Every fetched bar passes `validate_bar` first. A
   bar that fails never reaches the table — one bad print would otherwise
   corrupt every future replay silently. Quarantined bars are reported, never
   stored.

Weekends and holidays are NOT gaps: FX publishes no daily bar for Saturday,
so a missing Saturday is expected, not an error. The sync therefore works
from the latest STORED date forward and never demands a continuous calendar.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("tdapp.history")

TABLE = "price_history_daily"

#: A daily move bigger than this (vs the previous close) is quarantined, not
#: stored. Daily FX moves live in single-digit percents; 20% is either a bad
#: print or a peg break, and either way it must be looked at before it is
#: allowed to rewrite history.
SPIKE_PCT = 20.0


def bar_date_of(t: Any) -> str:
    """Unix seconds -> UTC 'YYYY-MM-DD'. Empty string when undatable."""
    try:
        ts = int(t)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def validate_bar(asset: str, bar_date: str, o: Any, h: Any, l: Any, c: Any,
                 prev_close: Optional[float] = None,
                 today: Optional[str] = None) -> list[str]:
    """Check ONE fetched bar. Returns error codes; empty = store it.

    Never raises — a validator that throws on a weird feed value would abort
    the whole sync, which is exactly the outage this must survive.
    """
    try:
        return _validate(asset, bar_date, o, h, l, c, prev_close, today)
    except Exception as exc:                       # noqa: BLE001 - quarantine, don't crash
        log.debug("validate %s %s failed closed: %s", asset, bar_date, exc)
        return ["validator_error"]


def _validate(asset, bar_date, o, h, l, c, prev_close, today) -> list[str]:
    errs: list[str] = []
    if not bar_date or not isinstance(bar_date, str):
        return ["no_date"]
    try:
        datetime.fromisoformat(bar_date).date()
    except ValueError:
        return ["bad_date"]
    if bar_date > (today or today_utc()):
        errs.append("future_bar")
    vals = {}
    for name, v in (("open", o), ("high", h), ("low", l), ("close", c)):
        try:
            f = float(v)
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
    if h_ < l_ - eps:
        errs.append("inverted_range")
    if h_ < max(o_, c_) - eps or l_ > min(o_, c_) + eps:
        errs.append("range_inconsistent")
    if prev_close:
        try:
            pc = float(prev_close)
            if pc > 0 and abs(c_ - pc) / pc * 100.0 > SPIKE_PCT:
                errs.append("spike_suspect")
        except (TypeError, ValueError):
            pass
    _ = asset  # reserved: per-asset sanity bands, if ever needed
    return errs


def _fetch(asset: str, days: int):
    """Default fetch: Yahoo-first candle chain, same as the live replay."""
    import httpx

    from app.integrations import quotes

    async def _go():
        async with httpx.AsyncClient() as client:
            return await quotes.fetch_candles(asset, client, days=days)

    return asyncio.run(_go())


def sync_asset(db, asset: str, days: int = 1095, fetch=None,
               _today: Optional[str] = None) -> dict[str, Any]:
    """Fetch only what is missing for one asset, validate, store. Never raises.

    Report: {asset, fetched, inserted, refreshed, skipped, invalid: [...],
    conflicts: [...], fresh: bool, error?: str}.
    """
    asset = str(asset or "").upper()
    rep: dict[str, Any] = {"asset": asset, "fetched": 0, "inserted": 0,
                           "refreshed": 0, "skipped": 0, "invalid": [],
                           "conflicts": [], "fresh": False}
    try:
        if not db or not db.available:
            rep["error"] = "DB unavailable"
            return rep
        stored = db.select(TABLE, filters={"asset": asset},
                           order="bar_date", desc=False, limit=5000) or []
    except Exception as exc:
        rep["error"] = f"read failed: {exc}"[:160]
        return rep

    have = {str(r.get("bar_date")): r for r in stored if r.get("bar_date")}
    latest = max(have) if have else ""
    today = _today or today_utc()

    if latest and latest >= today:
        # The stored history already covers today — the only bar that could
        # still move is today's, and re-fetching the whole span for it would
        # be precisely the waste this module exists to remove. Refresh just it
        # via a tiny span; anything older is frozen anyway.
        span = 7
    elif latest:
        try:
            gap = (datetime.fromisoformat(today).date()
                   - datetime.fromisoformat(latest).date()).days
        except ValueError:
            gap = int(days)
        span = max(7, min(int(days), gap + 10))
    else:
        span = int(days)

    try:
        candles = (fetch or _fetch)(asset, span)
    except Exception as exc:
        rep["error"] = f"fetch failed: {exc}"[:160]
        return rep

    rows = []
    for cnd in candles or []:
        d = bar_date_of(getattr(cnd, "t", 0))
        rows.append((d, getattr(cnd, "o", 0), getattr(cnd, "h", 0),
                     getattr(cnd, "l", 0), getattr(cnd, "c", 0)))
    rows.sort(key=lambda r: r[0])
    rep["fetched"] = len(rows)

    prev_close: Optional[float] = None
    for d, o, h, l, c in rows:
        errs = validate_bar(asset, d or "", o, h, l, c, prev_close, today)
        try:
            if not errs:
                prev_close = float(c)
        except (TypeError, ValueError):
            pass
        if errs:
            if d:
                rep["invalid"].append({"bar_date": d, "reasons": errs})
            else:
                rep["invalid"].append({"bar_date": "", "reasons": errs})
            continue
        old = have.get(d)
        if old is None:
            if _insert(db, asset, d, o, h, l, c):
                rep["inserted"] += 1
                have[d] = {"bar_date": d}
            else:
                rep["invalid"].append({"bar_date": d,
                                       "reasons": ["write_failed"]})
        elif d == latest and d >= today:
            # The forming bar: refresh, never touch older ones.
            try:
                db.delete(TABLE, {"asset": asset, "bar_date": d})
                if _insert(db, asset, d, o, h, l, c):
                    rep["refreshed"] += 1
                else:
                    rep["invalid"].append({"bar_date": d,
                                           "reasons": ["write_failed"]})
            except Exception as exc:
                rep["invalid"].append({"bar_date": d,
                                       "reasons": [f"refresh_failed: {exc}"[:80]]})
        else:
            try:
                same = all(abs(float(old.get(k) or 0) - float(v)) < 1e-9
                           for k, v in (("open", o), ("high", h),
                                        ("low", l), ("close", c)))
            except (TypeError, ValueError):
                same = False
            if same:
                rep["skipped"] += 1
            else:
                rep["conflicts"].append({
                    "bar_date": d,
                    "stored": [old.get("open"), old.get("high"),
                               old.get("low"), old.get("close")],
                    "fetched": [o, h, l, c],
                })

    if latest and latest >= today and not rep["fetched"]:
        rep["fresh"] = True
    return rep


def _insert(db, asset: str, d: str, o: float, h: float,
            l: float, c: float) -> bool:
    try:
        return bool(db.insert(TABLE, {
            "asset": asset, "bar_date": d,
            "open": round(float(o), 6), "high": round(float(h), 6),
            "low": round(float(l), 6), "close": round(float(c), 6),
            "source": "yahoo",
        }))
    except Exception as exc:
        log.debug("history insert %s %s failed: %s", asset, d, exc)
        return False


def load_series(db, assets, days: int = 1095) -> tuple[dict, dict]:
    """Sync every asset, then read back full series oldest-first.

    Returns (series, report). `series` maps asset -> Candle list (with `t`,
    so the replay is byte-identical to a live fetch). `report` carries the
    per-asset validation outcome for the run row. Never raises.
    """
    from app.integrations.quotes import Candle

    series: dict[str, list] = {}
    per: dict[str, Any] = {}
    totals = {"fetched": 0, "inserted": 0, "refreshed": 0, "skipped": 0,
              "invalid": 0, "conflicts": 0}
    for asset in sorted(set(str(a).upper() for a in assets or [])):
        try:
            rep = sync_asset(db, asset, days)
        except Exception as exc:                   # noqa: BLE001 - one bad asset skips
            per[asset] = {"error": str(exc)[:120]}
            continue
        per[asset] = {k: (len(v) if isinstance(v, list) else v)
                      for k, v in rep.items() if k != "asset"}
        for k in totals:
            v = rep.get(k)
            totals[k] += len(v) if isinstance(v, list) else int(v or 0)
        try:
            rows = db.select(TABLE, filters={"asset": asset},
                             order="bar_date", desc=False,
                             limit=max(int(days), 220)) or []
        except Exception as exc:
            per[asset] = {"error": f"readback failed: {exc}"[:120]}
            continue
        candles = []
        for r in rows:
            try:
                t = int(datetime.fromisoformat(
                    str(r["bar_date"])).replace(
                        tzinfo=timezone.utc).timestamp())
                candles.append(Candle(o=float(r["open"]), h=float(r["high"]),
                                      l=float(r["low"]), c=float(r["close"]),
                                      t=t))
            except (KeyError, TypeError, ValueError):
                continue
        if candles:
            series[asset] = candles
    return series, {"per_asset": per, "totals": totals,
                    "source": "price_history_daily (incremental)"}
