"""Quote API call log — every external price fetch, recorded + auto-purged.

WHY: the user wants full visibility into WHERE prices come from. Every
HTTP call the quote layer makes (exchangerate-api.com, Yahoo chart,
Frankfurter, Twelve Data) is logged with:
  - the exact URL (api key stripped from the query string)
  - success/error + HTTP status + error text
  - the price returned (when any)
  - latency (duration_ms)
  - a MASKED api key hint: first half + "…" (second half never stored)

RETENTION: rows older than QUOTE_LOG_TTL_DAYS (7) are deleted automatically.
Purging is throttled (at most once per PURGE_INTERVAL_S) and runs inline on
insert + from GET /api/system/quote-logs. Never raises — logging must not
break the price path.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

QUOTE_LOG_TTL_DAYS = 7
PURGE_INTERVAL_S = 300.0  # purge at most every 5 min
_last_purge = 0.0

TABLE = "quote_api_logs"

# asset → category for the summary card (forex vs gold)
GOLD_ASSETS = {"XAUUSD"}

# The Database instance is injected once at startup (main.py lifespan) —
# quotes.py must not import app.state, so it logs through this module.
_db: Any = None


def set_db(db: Any) -> None:
    """Inject the Database used for logging (called from main.py lifespan)."""
    global _db
    _db = db


def get_db() -> Any:
    return _db


def category_for(asset: str) -> str:
    return "gold" if str(asset).upper() in GOLD_ASSETS else "forex"


def mask_key(key: str) -> str:
    """First half + '…' — the second half is never stored or shown."""
    k = (key or "").strip()
    if not k:
        return ""
    half = max(1, len(k) // 2)
    return k[:half] + "…"


def strip_key_from_url(url: str) -> str:
    """Remove apikey=... from a URL (Twelve Data puts the key in the query)."""
    if "apikey=" not in url:
        return url
    head, _, tail = url.partition("apikey=")
    tail = tail.split("&", 1)[1] if "&" in tail else ""
    return head + "apikey=…" + ("&" + tail if tail else "")


def log_call(*, asset: str, category: str, provider: str,
             url: str, status: str, http_status: Optional[int] = None,
             price: Optional[float] = None, error: str = "",
             duration_ms: Optional[int] = None,
             api_key_hint: str = "") -> None:
    """Persist one call row. Never raises; silently skips when DB is down."""
    try:
        db = _db
        if db is None or not getattr(db, "available", False):
            return
        db.insert(TABLE, {
            "asset": str(asset or ""),
            "category": category,
            "provider": provider,
            "url": strip_key_from_url(str(url or "")),
            "api_key_hint": api_key_hint,
            "status": status,
            "http_status": http_status,
            "price": round(price, 5) if price is not None else None,
            "error": str(error or "")[:500],
            "duration_ms": duration_ms,
        })
    except Exception as exc:
        log.debug("quote log insert failed: %s", exc)


def purge_old_logs(db: Any, force: bool = False) -> int:
    """Delete rows older than 7 days. Throttled unless force=True.

    Returns the number of rows deleted (0 when skipped/unavailable).
    Prefers one bulk delete_before (lt created_at cutoff); falls back to
    per-row deletes when the client lacks delete_before (FakeDatabase).
    """
    global _last_purge
    now = time.monotonic()
    if not force and now - _last_purge < PURGE_INTERVAL_S:
        return 0
    _last_purge = now
    try:
        if db is None or not getattr(db, "available", False):
            return 0
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=QUOTE_LOG_TTL_DAYS)).isoformat()
        bulk = getattr(db, "delete_before", None)
        if bulk is not None:
            return int(bulk(TABLE, "created_at", cutoff) or 0)
        rows = db.select(TABLE, filters={}, order="created_at", desc=True,
                         limit=1000)
        stale = [r for r in rows if str(r.get("created_at") or "") < cutoff]
        deleted = 0
        for r in stale:
            if not r.get("id"):
                continue
            if db.delete(TABLE, {"id": r["id"]}):
                deleted += 1
        if deleted:
            log.info("quote_api_logs: purged %d rows older than %d days",
                     deleted, QUOTE_LOG_TTL_DAYS)
        return deleted
    except Exception as exc:
        log.debug("quote log purge failed: %s", exc)
        return 0


def summary(db: Any) -> dict[str, Any]:
    """Aggregate counts for the summary card (7-day window).

    Returns {total, success, error, forex: {total, success, error},
    gold: {...}, by_provider: {provider: {total, success, error}}}.
    """
    out: dict[str, Any] = {
        "total": 0, "success": 0, "error": 0,
        "forex": {"total": 0, "success": 0, "error": 0},
        "gold": {"total": 0, "success": 0, "error": 0},
        "by_provider": {},
    }
    try:
        if db is None or not getattr(db, "available", False):
            return out
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=QUOTE_LOG_TTL_DAYS))
        # paging read — a single select(limit=1000) silently truncates once
        # the table grows past 1000 rows and the cards undercount
        rows = db.select_paged(TABLE, order="created_at", desc=True)
        for r in rows:
            created = str(r.get("created_at") or "")
            try:
                row_date = datetime(
                    int(created[0:4]), int(created[5:7]), int(created[8:10]),
                    tzinfo=timezone.utc)
            except (ValueError, IndexError):
                continue
            if row_date.date() < cutoff.date():
                continue
            cat = r.get("category") if r.get("category") in ("forex", "gold") else "forex"
            prov = str(r.get("provider") or "unknown")
            ok = r.get("status") == "success"
            out["total"] += 1
            out["success" if ok else "error"] += 1
            bucket = out[cat]
            bucket["total"] += 1
            bucket["success" if ok else "error"] += 1
            p = out["by_provider"].setdefault(
                prov, {"total": 0, "success": 0, "error": 0})
            p["total"] += 1
            p["success" if ok else "error"] += 1
    except Exception as exc:
        log.debug("quote log summary failed: %s", exc)
    return out
