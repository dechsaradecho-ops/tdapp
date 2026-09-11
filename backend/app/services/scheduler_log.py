"""Scheduler run log — every APScheduler job tick, recorded + auto-purged.

WHY: workers run silently in-process (Render tdapp-api, ENABLE_WORKERS=1).
When SL doesn't move or no signals appear, the user can't tell whether the
scheduler itself is alive. This service logs one row per job run to
`scheduler_runs` (migration 030):

    job_id      market_scanner | news_analysis | portfolio_monitor |
                notifications | auto_trader | position_guard |
                calendar_sync | daily_digest
    status      ok | error
    duration_ms wall time of the tick
    detail      short result summary (truncated, e.g. guard_once counters)
    error       exception text when status=error

RETENTION: rows older than SCHEDULER_LOG_TTL_DAYS (7) are deleted
automatically (throttled, runs from GET /api/system/scheduler-logs).
Never raises — logging must not break the scheduler.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

SCHEDULER_LOG_TTL_DAYS = 7
PURGE_INTERVAL_S = 300.0  # purge at most every 5 min
_last_purge = 0.0

TABLE = "scheduler_runs"


def _summarize(result: Any, limit: int = 480) -> str:
    """Compact one-line summary of a job return value (never raises).

    ``limit`` mirrors the 500-char cap in ``log_run``: guard_once now also
    returns symbol lists (sl_assets / closed_assets / skip_assets) so the
    Logs > Guard tab can name WHICH pair moved its stop, not just how many.
    A 300-char cap silently dropped those lists off the end — the counters
    are short, the audit lists are not.
    """
    try:
        if result is None:
            return ""
        if isinstance(result, dict):
            parts = [f"{k}={v}" for k, v in list(result.items())[:12]]
            return (", ".join(parts))[:limit]
        return str(result)[:limit]
    except Exception:
        return ""


def log_run(*, db: Any = None, job_id: str = "", status: str = "ok",
            duration_ms: int | None = None, detail: str = "",
            error: str = "") -> None:
    """Persist one scheduler tick row. Never raises; skips when DB is down."""
    try:
        if db is None or not getattr(db, "available", False):
            return
        if status not in ("ok", "error"):
            status = "ok"
        db.insert(TABLE, {
            "job_id": str(job_id or "")[:80],
            "status": status,
            "duration_ms": int(duration_ms) if duration_ms is not None else None,
            "detail": str(detail or "")[:500],
            "error": str(error or "")[:500],
        })
    except Exception as exc:
        log.debug("scheduler log insert failed: %s", exc)


def purge_old_logs(db: Any, force: bool = False) -> int:
    """Delete rows older than 7 days. Throttled unless force=True."""
    global _last_purge
    now = time.monotonic()
    if not force and now - _last_purge < PURGE_INTERVAL_S:
        return 0
    _last_purge = now
    try:
        if db is None or not getattr(db, "available", False):
            return 0
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=SCHEDULER_LOG_TTL_DAYS)).isoformat()
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
        return deleted
    except Exception as exc:
        log.debug("scheduler log purge failed: %s", exc)
        return 0


def summary(db: Any) -> dict:
    """Aggregate the last-7-days runs by job/status for the log page header.

    Counts via Database.count (PostgREST count=exact) — exact at ANY table
    size. Falls back to a capped scan for legacy fakes without count().
    """
    out: dict[str, Any] = {
        "total": 0, "ok": 0, "error": 0, "by_job": {},
    }
    try:
        if db is None or not getattr(db, "available", False):
            return out
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=SCHEDULER_LOG_TTL_DAYS)).isoformat()
        count = getattr(db, "count", None)
        if count is None:
            rows = db.select_paged(TABLE, filters={}, order="created_at",
                                   desc=True)
            rows = [r for r in rows
                    if str(r.get("created_at") or "") >= cutoff]
            by_job: dict[str, dict] = {}
            for r in rows:
                job = str(r.get("job_id") or "?")
                ok = r.get("status") == "ok"
                out["total"] += 1
                out["ok" if ok else "error"] += 1
                b = by_job.setdefault(job, {"total": 0, "ok": 0, "error": 0})
                b["total"] += 1
                b["ok" if ok else "error"] += 1
            out["by_job"] = dict(sorted(by_job.items()))
            return out
        out["total"] = count(TABLE, created_after=cutoff) or 0
        out["ok"] = count(TABLE, filters={"status": "ok"},
                          created_after=cutoff) or 0
        out["error"] = out["total"] - out["ok"]
        try:
            recent = db.select(TABLE, filters={}, order="created_at",
                               desc=True, limit=500, columns="job_id")
        except TypeError:  # older fake select() without `columns`
            recent = db.select(TABLE, filters={}, order="created_at",
                               desc=True, limit=500)
        for job in sorted({str(r.get("job_id") or "?") for r in recent}):
            total_j = count(TABLE, filters={"job_id": job},
                            created_after=cutoff) or 0
            if not total_j:
                continue
            ok_j = count(TABLE, filters={"job_id": job, "status": "ok"},
                         created_after=cutoff) or 0
            out["by_job"][job] = {"total": total_j, "ok": ok_j,
                                  "error": total_j - ok_j}
        return out
    except Exception as exc:
        log.debug("scheduler log summary failed: %s", exc)
        return out
