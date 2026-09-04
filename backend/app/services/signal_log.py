"""Signal lifecycle log — every signal event, recorded + auto-purged.

WHY: the `signals` table keeps only the LATEST state per row. Once a signal
is approved, blocked, expires or its position closes, the WHY is lost. This
service logs each lifecycle event to `signal_logs` (migration 013):

    created        scanner generated the signal (with its reasons)
    order_opened   order actually placed (ticket + volume)
    order_blocked  gate said NO (pause/limits/news/correlation/risk-officer,
                   broker rejection, bad sizing) — reason stored
    rejected       user pressed ปฏิเสธ (semi-auto)
    expired        pending past the 30-min TTL
    closed         position closed (SL/TP/manual) — exit price + PnL

RETENTION: rows older than SIGNAL_LOG_TTL_DAYS (7) are deleted automatically.
Purging is throttled (at most once per PURGE_INTERVAL_S) and runs from
GET /api/system/signal-logs. Never raises — logging must not break trading.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

SIGNAL_LOG_TTL_DAYS = 7
PURGE_INTERVAL_S = 300.0  # purge at most every 5 min
_last_purge = 0.0

TABLE = "signal_logs"

EVENTS = ("created", "order_opened", "order_blocked", "rejected", "expired", "closed")


def log_event(*, event: str, signal_id: str = "", asset: str = "",
              direction: str = "", confidence: Optional[float] = None,
              entry: Optional[float] = None, stop_loss: Optional[float] = None,
              take_profit: Optional[float] = None, source: str = "",
              reason: str = "", ticket: str = "",
              volume: Optional[float] = None, pnl: Optional[float] = None,
              exit_price: Optional[float] = None, db: Any = None) -> None:
    """Persist one lifecycle row. Never raises; silently skips when DB is down.

    The db handle is passed explicitly (unlike quote_log's module-level
    injection) because callers span workers, the execution service and
    routes — every one of them already holds a db reference.
    """
    if db is None or not getattr(db, "available", False):
        return
    if event not in EVENTS:
        event = "created"
    sid = str(signal_id or "")
    if event == "order_blocked" and sid:
        # Dedup: the auto-trader re-evaluates the same pending signal every
        # minute, so a blocked signal would spam signal-logs with an identical
        # ⛔ row each cycle. Log the FIRST block per signal only — the reason
        # rarely changes between cycles, and the row stays for the 7-day TTL.
        try:
            existing = db.select(TABLE, filters={"signal_id": sid,
                                                 "event": "order_blocked"},
                                 order="created_at", desc=True, limit=1)
            if existing:
                log.debug("order_blocked for %s already logged — skipping", sid)
                return
        except Exception as exc:
            log.debug("order_blocked dedup check failed (logging anyway): %s", exc)
    row = {
        "signal_id": sid,
        "asset": str(asset or ""),
        "direction": str(direction or "").lower(),
        "event": event,
        "source": str(source or ""),
        "reason": str(reason or "")[:500],
        "ticket": str(ticket or ""),
    }
    for k, v in (("confidence", confidence), ("entry", entry),
                 ("stop_loss", stop_loss), ("take_profit", take_profit),
                 ("volume", volume), ("pnl", pnl), ("exit_price", exit_price)):
        if v is not None:
            row[k] = round(float(v), 5)
    try:
        db.insert(TABLE, row)
    except Exception as exc:
        log.debug("signal log insert failed: %s", exc)


def purge_old_logs(db: Any, force: bool = False) -> int:
    """Delete rows older than 7 days. Throttled unless force=True.

    Returns the number of rows deleted (0 when skipped/unavailable).
    Prefers one bulk delete_before; falls back to per-row deletes when the
    client lacks delete_before (FakeDatabase).
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
                  - timedelta(days=SIGNAL_LOG_TTL_DAYS)).isoformat()
        bulk = getattr(db, "delete_before", None)
        if bulk is not None:
            return int(bulk(TABLE, "created_at", cutoff) or 0)
        rows = db.select(TABLE, filters={}, order="created_at", desc=True,
                         limit=1000)
        stale = [r for r in rows if str(r.get("created_at") or "") < cutoff]
        deleted = 0
        for r in stale:
            if db.delete(TABLE, {"id": r["id"]}):
                deleted += 1
        return deleted
    except Exception as exc:
        log.debug("signal log purge failed: %s", exc)
        return 0


def summary(db: Any) -> dict:
    """Aggregate the last-7-days rows by event for the log page header."""
    out: dict[str, Any] = {
        "total": 0, "by_event": {}, "by_asset": {},
        "opened": 0, "blocked": 0, "expired": 0, "rejected": 0, "closed": 0,
    }
    try:
        if db is None or not getattr(db, "available", False):
            return out
        rows = db.select(TABLE, filters={}, order="created_at", desc=True,
                         limit=1000)
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=SIGNAL_LOG_TTL_DAYS)).isoformat()
        rows = [r for r in rows
                if str(r.get("created_at") or "") >= cutoff]
        out["total"] = len(rows)
        by_event: dict[str, int] = {}
        by_asset: dict[str, int] = {}
        for r in rows:
            ev = str(r.get("event") or "?")
            by_event[ev] = by_event.get(ev, 0) + 1
            asset = str(r.get("asset") or "?")
            by_asset[asset] = by_asset.get(asset, 0) + 1
            if ev == "order_opened":
                out["opened"] += 1
            elif ev == "order_blocked":
                out["blocked"] += 1
            elif ev == "expired":
                out["expired"] += 1
            elif ev == "rejected":
                out["rejected"] += 1
            elif ev == "closed":
                out["closed"] += 1
        out["by_event"] = dict(sorted(by_event.items(),
                                      key=lambda kv: kv[1], reverse=True))
        out["by_asset"] = dict(sorted(by_asset.items(),
                                      key=lambda kv: kv[1], reverse=True))
        return out
    except Exception as exc:
        log.debug("signal log summary failed: %s", exc)
        return out
