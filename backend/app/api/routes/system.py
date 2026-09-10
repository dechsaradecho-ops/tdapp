"""Live DB read/write probe — proves (or disproves) that the backend can
actually INSERT + SELECT in Supabase, surfacing RLS/policy errors that the
silent-failure Database wrapper swallows by design.

Route: GET /api/system/db-check
  - inserts a row into the `db_probe` table with a random token
  - selects it back by token
  - deletes it
  - returns each step's status + raw error (if any)

The `db_probe` table is intentionally simple (no RLS, no FKs). Create it with
database/003_db_probe.sql once. If the table is missing the endpoint reports
that instead of failing.

Also: GET /api/system/counts → live row counts for the 5 worker tables.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request

from app.services.database import Database
from app.services import execution

router = APIRouter()

PROBE_TABLE = "db_probe"


def _ok(db: Database) -> dict:
    return {"table": PROBE_TABLE, "client": "ok" if db.available else "unavailable"}


@router.get("/db-check")
async def db_check(request: Request) -> dict:
    """Insert → select → delete one probe row; report every raw error."""
    db: Database = request.app.state.db
    result: dict[str, Any] = _ok(db)
    if not db.available:
        result["verdict"] = "fail"
        result["error"] = db.init_error or "client unavailable"
        return result

    token = f"probe-{uuid.uuid4().hex[:12]}"

    # -- 1. INSERT ------------------------------------------------------
    ins = db.insert(PROBE_TABLE, {"token": token, "note": "db-check"})
    result["insert"] = "ok" if ins else "FAIL"
    if not ins:
        result["verdict"] = "fail"
        result["insert_hint"] = (
            "insert returned None — the raw error is in the backend logs "
            "(search 'insert db_probe failed'). Common causes: table missing "
            "(run database/003_db_probe.sql) or RLS blocking the service key."
        )
        return result

    # -- 2. SELECT back by token ----------------------------------------
    rows = db.select(PROBE_TABLE, filters={"token": token}, limit=1)
    result["select"] = "ok" if rows else "FAIL"
    if not rows:
        result["verdict"] = "fail"
        result["select_hint"] = "inserted row not found by token — check backend logs"
        return result

    # -- 3. DELETE cleanup -----------------------------------------------
    deleted = db.delete(PROBE_TABLE, {"token": token})
    result["delete"] = "ok" if deleted else "FAIL"
    result["token"] = token
    ok_probe = ins and rows and deleted

    # -- 4. WORKER-TABLE PROBE (scanner-shaped INSERT into market_analysis)
    # db_probe has no RLS → proves connectivity but NOT the RLS policies that
    # gate the worker tables. This inserts a row exactly like the market
    # scanner would and reports the RAW PostgREST error when it fails.
    worker_row = {
        "asset": "PROBE",
        "regime": "sideway",           # valid market_regime enum value
        "sentiment": "neutral",
        "confidence": 50.00,
        "explanation": "db-check probe row — safe to delete",
    }
    data, raw_err = db.insert_raw("market_analysis", worker_row)
    result["worker_insert"] = "ok" if data else "FAIL"
    result["worker_table"] = "market_analysis"
    if not data:
        result["worker_insert_error"] = raw_err
        result["worker_insert_hint"] = (
            "scanner-shaped INSERT into market_analysis failed. If the error "
            "mentions row-level security / policy, run "
            "database/004_rls_insert_policies.sql in the Supabase SQL Editor."
        )
    else:
        # cleanup the probe row immediately
        db.delete("market_analysis", {"asset": "PROBE"})

    result["verdict"] = "pass" if (ok_probe and data) else "partial"
    return result


@router.get("/counts")
async def counts(request: Request) -> dict:
    """Row counts for the worker tables (verifies persistence is flowing)."""
    db: Database = request.app.state.db
    out: dict[str, Any] = {"client": "ok" if db.available else "unavailable"}
    if not db.available:
        out["verdict"] = "fail"
        out["error"] = db.init_error or "client unavailable"
        return out
    for table, order_col in (("market_analysis", "created_at"),
                             ("signals", "created_at"),
                             ("news_analysis", "created_at"),
                             ("trades", "created_at")):
        rows = db.select(table, order=order_col, desc=True, limit=100)
        out[table] = len(rows)
        latest = rows[0].get("created_at") if rows else None
        if latest:
            out[f"{table}_latest"] = latest
    out["verdict"] = "ok"
    return out


@router.post("/scan-now")
async def scan_now(request: Request) -> dict:
    """Trigger one market-scanner cycle immediately (no 5-min wait).

    Answers 'when does it actually insert?' — runs the same scan_once the
    scheduler runs, but inline, and reports per-step outcomes plus any raw
    error the scanner's silent-failure path would normally swallow.
    """
    db: Database = request.app.state.db
    out: dict[str, Any] = {"client": "ok" if db.available else "unavailable"}
    if not db.available:
        out["verdict"] = "fail"
        out["error"] = db.init_error or "client unavailable"
        return out

    from app.workers.market_scanner import scan_once

    try:
        results = await scan_once(db)
        out["scanned"] = len(results)
        out["assets"] = [
            {"asset": r["asset"], "score": r["opportunity"]["score"],
             "source": r["snapshot"].get("source")}
            for r in results
        ]
    except Exception as exc:  # surface anything the worker swallowed
        out["verdict"] = "fail"
        out["error"] = f"{exc.__class__.__name__}: {exc}"
        return out

    rows = db.select("market_analysis", order="created_at", desc=True, limit=100)
    out["market_analysis_rows"] = len(rows)
    out["market_analysis_latest"] = rows[0].get("created_at") if rows else None
    signals = db.select("signals", order="created_at", desc=True, limit=100)
    out["signals_rows"] = len(signals)
    out["verdict"] = "ok" if rows else "fail"
    if not rows:
        out["hint"] = ("scan ran but market_analysis is still empty — the "
                       "insert failed; run /api/system/db-check for the raw error")
    return out


@router.post("/autotrader-dry-run")
async def autotrader_dry_run(request: Request) -> dict:
    """Run ONE auto-trader cycle inline and report every gate verdict.

    The scheduler's trade_once only logs blocks — this endpoint surfaces the
    same information over HTTP: which pending signals were picked, whether
    each passed the gate (pause/kill/frequency/news/correlation/risk-officer),
    and the broker result. Use it to answer 'why is nothing firing?'.
    """
    app = request.app
    db: Database = app.state.db
    out: dict[str, Any] = {"client": "ok" if db.available else "unavailable"}
    if not db.available:
        out["verdict"] = "fail"
        out["error"] = db.init_error or "client unavailable"
        return out

    pending = db.select("signals", filters={"approval": "pending"}, limit=10)
    out["pending_signals"] = [
        {"id": r.get("id"), "asset": r.get("asset"),
         "direction": r.get("direction"), "confidence": r.get("confidence"),
         "created_at": r.get("created_at")}
        for r in pending
    ]

    from app.workers.auto_trader import trade_once
    try:
        out["trade_once"] = await trade_once(
            db, app.state.broker, app.state.line)
    except Exception as exc:  # surface anything the worker swallowed
        out["verdict"] = "fail"
        out["error"] = f"{exc.__class__.__name__}: {exc}"
        return out

    s = execution.get_app_settings(db)
    out["order_mode"] = s.order_mode
    out["capital"] = s.capital

    # Post-state: did anything actually open?
    open_rows = db.select("paper_trades", filters={"status": "open"}, limit=50)
    out["open_paper_trades"] = len(open_rows)
    out["open_detail"] = [
        {"asset": r.get("asset"), "direction": r.get("direction"),
         "volume": r.get("volume"), "ticket": r.get("ticket"),
         "created_at": r.get("created_at")}
        for r in open_rows
    ]
    out["verdict"] = "ok"
    return out


# ---------------------------------------------------------------------------
# Quote API call log — every external price fetch (7-day auto-expiry)
# ---------------------------------------------------------------------------
@router.get("/quote-logs")
async def quote_logs(request: Request, limit: int = 100, offset: int = 0,
                     category: str = "all", provider: str = "all") -> dict:
    """Recent quote-API calls + summary card data (forex vs gold).

    Server-side paging: `offset` skips rows so the Logs page can walk the
    full 7-day window in 500-row chunks — the old fetch-500-recent-only
    shape hid everything older. `limit` stays capped at 500 per request
    (one PostgREST round trip, no 20000-row dump). `category`/`provider`
    filters run server-side so paging stays consistent while filtering.
    Rows older than 7 days are purged automatically (throttled to once per
    5 min; force=True here so opening the page always cleans up).
    """
    db: Database = request.app.state.db
    out: dict[str, Any] = {"client": "ok" if db.available else "unavailable"}
    if not db.available:
        out["verdict"] = "fail"
        out["error"] = db.init_error or "client unavailable"
        return out

    from app.services import quote_log
    quote_log.purge_old_logs(db, force=True)
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    filters: dict[str, Any] = {}
    if category in ("forex", "gold"):
        filters["category"] = category
    if provider and provider != "all":
        filters["provider"] = provider
    rows = db.select(quote_log.TABLE, filters=filters, order="created_at",
                     desc=True, limit=page_size, offset=page_offset)
    out["logs"] = [
        {
            "id": r.get("id"),
            "created_at": r.get("created_at"),
            "asset": r.get("asset"),
            "category": r.get("category"),
            "provider": r.get("provider"),
            "url": r.get("url"),
            "api_key_hint": r.get("api_key_hint"),
            "status": r.get("status"),
            "http_status": r.get("http_status"),
            "price": r.get("price"),
            "error": r.get("error"),
            "duration_ms": r.get("duration_ms"),
        }
        for r in rows
    ]
    out["summary"] = quote_log.summary(db)
    out["ttl_days"] = quote_log.QUOTE_LOG_TTL_DAYS
    out["offset"] = page_offset
    out["limit"] = page_size
    # exact total for the active filter (cheap count=exact, no row fetch)
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=quote_log.QUOTE_LOG_TTL_DAYS)).isoformat()
        total = db.count(quote_log.TABLE, filters=filters or None,
                         created_after=cutoff)
        out["total"] = total if total is not None else len(rows)
    except Exception:
        out["total"] = len(rows)
    out["has_more"] = (page_offset + len(rows)) < (out["total"] or 0)
    out["verdict"] = "ok"
    return out


# ---------------------------------------------------------------------------
# Signal lifecycle log — created/blocked/opened/rejected/expired/closed (7-day)
# ---------------------------------------------------------------------------
_SIGLOG_SUMMARY_TS: float = 0.0
_SIGLOG_SUMMARY_TTL_S = 60.0
_SIGLOG_SUMMARY_CACHE: dict[str, Any] = {}


@router.get("/signal-logs")
async def signal_logs(request: Request, limit: int = 100, offset: int = 0,
                      event: str = "all") -> dict:
    """Recent signal lifecycle events + summary card data.

    Server-side paging like /quote-logs: `offset` skips rows so the signal
    tab can walk the full 7-day window in 500-row chunks; `limit` capped at
    500 per request. `event` filters server-side (one of
    created/order_opened/order_blocked/rejected/expired/closed).

    ทุกสัญญาณจะถูกบันทึกตั้งแต่เกิด (created) จนจบชะตา (opened / blocked /
    rejected / expired / closed) เก็บย้อนหลัง 7 วัน — rows เก่าถูก purge
    อัตโนมัติ (throttled 5 นาที; force=True ตอนเปิดหน้าเพื่อ cleanup เสมอ)
    """
    db: Database = request.app.state.db
    out: dict[str, Any] = {"client": "ok" if db.available else "unavailable"}
    if not db.available:
        out["verdict"] = "fail"
        out["error"] = db.init_error or "client unavailable"
        return out

    from app.services import signal_log
    signal_log.purge_old_logs(db, force=True)
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    filters: dict[str, Any] = {}
    if event in signal_log.EVENTS:
        filters["event"] = event
    # summary หนักสุด (count=exact หลายรอบ) — memo 60 วิฝั่ง process:
    # summary ไม่ขึ้นกับ event/offset (นับทั้งหน้าต่าง 7 วันเหมือนกันทุก
    # filter) จึง reuse ข้าม filter/page-turn ได้
    global _SIGLOG_SUMMARY_TS, _SIGLOG_SUMMARY_CACHE
    now_ts = time.monotonic()
    if (now_ts - _SIGLOG_SUMMARY_TS < _SIGLOG_SUMMARY_TTL_S
            and _SIGLOG_SUMMARY_CACHE):
        summary_data: dict[str, Any] = dict(_SIGLOG_SUMMARY_CACHE)
    else:
        summary_data = signal_log.summary(db)
        _SIGLOG_SUMMARY_CACHE = dict(summary_data)
        _SIGLOG_SUMMARY_TS = now_ts
    rows = db.select(signal_log.TABLE, filters=filters, order="created_at",
                     desc=True, limit=page_size, offset=page_offset)
    out["logs"] = [
        {
            "id": r.get("id"),
            "created_at": r.get("created_at"),
            "signal_id": r.get("signal_id"),
            "asset": r.get("asset"),
            "direction": r.get("direction"),
            "event": r.get("event"),
            "confidence": r.get("confidence"),
            "entry": r.get("entry"),
            "stop_loss": r.get("stop_loss"),
            "take_profit": r.get("take_profit"),
            "source": r.get("source"),
            "reason": r.get("reason"),
            "ticket": r.get("ticket"),
            "volume": r.get("volume"),
            "pnl": r.get("pnl"),
            "exit_price": r.get("exit_price"),
        }
        for r in rows
    ]
    out["summary"] = summary_data
    out["ttl_days"] = signal_log.SIGNAL_LOG_TTL_DAYS
    out["offset"] = page_offset
    out["limit"] = page_size
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=signal_log.SIGNAL_LOG_TTL_DAYS)).isoformat()
        total = db.count(signal_log.TABLE, filters=filters or None,
                         created_after=cutoff)
        out["total"] = total if total is not None else len(rows)
    except Exception:
        out["total"] = len(rows)
    out["has_more"] = (page_offset + len(rows)) < (out["total"] or 0)
    out["verdict"] = "ok"
    return out


# ---------------------------------------------------------------------------
# News analysis history — worker #2 output (headline + AI sentiment per event)
# ---------------------------------------------------------------------------
@router.get("/news-logs")
async def news_logs(request: Request, limit: int = 100, offset: int = 0,
                    event: str = "all") -> dict:
    """Recent news-analysis rows for the Logs page news tab.

    Server-side paging like /quote-logs: `offset` skips rows, `limit` capped
    at 500 per request, `event` filters server-side (one of the worker's
    EVENT_TYPES). Each row = one worker cycle: event type, AI sentiment
    (-1..+1), affected assets, Thai analysis (+ real headline when the feed
    works) and confidence. Read-only over the existing news_analysis table —
    no new table or migration needed. Never raises.
    """
    db: Database = request.app.state.db
    out: dict[str, Any] = {"client": "ok" if db.available else "unavailable"}
    if not db.available:
        out["verdict"] = "fail"
        out["error"] = db.init_error or "client unavailable"
        return out

    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    filters: dict[str, Any] = {}
    if event and event != "all":
        filters["event"] = event
    rows = db.select("news_analysis", filters=filters, order="created_at",
                     desc=True, limit=page_size, offset=page_offset)
    logs = [
        {
            "id": r.get("id"),
            "created_at": r.get("created_at"),
            "event": r.get("event"),
            "sentiment": r.get("sentiment"),
            "affected_assets": r.get("affected_assets") or [],
            "analysis": r.get("analysis"),
            "confidence": r.get("confidence"),
        }
        for r in rows
    ]
    try:
        total = db.count("news_analysis", filters=filters or None)
        total_n = total if total is not None else len(rows)
    except Exception:
        total_n = len(rows)
    # window counts for the header cards — fixed 5-event set, one tiny
    # count request each (exact at any table size, no row fetch)
    by_event: dict[str, int] = {}
    try:
        from app.workers.news_analysis import EVENT_TYPES
        for ev in EVENT_TYPES:
            n = db.count("news_analysis", filters={"event": ev})
            if n:
                by_event[ev] = n
    except Exception:
        by_event = {}
        for r in rows:
            ev = str(r.get("event") or "?")
            by_event[ev] = by_event.get(ev, 0) + 1
    if not by_event:
        for r in rows:
            ev = str(r.get("event") or "?")
            by_event[ev] = by_event.get(ev, 0) + 1
    out["logs"] = logs
    out["offset"] = page_offset
    out["limit"] = page_size
    out["total"] = total_n
    out["has_more"] = (page_offset + len(rows)) < (total_n or 0)
    # real-vs-heuristic split is computed over the fetched page only
    # (legacy shape returned the same window over its ≤500 rows)
    real = sum(1 for r in rows
               if r.get("analysis") and "heuristic" not in str(r.get("analysis")))
    out["summary"] = {"total": total_n, "by_event": by_event,
                      "real": real, "heuristic": len(rows) - real}
    out["verdict"] = "ok"
    return out


@router.post("/quote-test")
async def quote_test(request: Request) -> dict:
    """Force-fetch live prices for ALL assets (bypasses the 30s cache).

    Exercises the real chain (exchangerate-api → Yahoo fallback) so the user
    can verify the feed from the UI; every underlying HTTP call lands in the
    quote log. Never raises — failures are reported per asset.
    """
    from app.integrations import quotes
    from app.services import quote_log

    assets = sorted(quotes.YAHOO_SYMBOLS)  # EURUSD GBPUSD USDJPY AUDUSD XAUUSD
    quotes._spot_cache.clear()  # force real HTTP calls, not the 30s cache
    try:
        prices, failures = await quotes.fetch_spot_prices(assets)
    except Exception as exc:
        return {
            "verdict": "fail",
            "error": f"{exc.__class__.__name__}: {exc}",
            "prices": {}, "failures": {a: str(exc) for a in assets},
        }
    return {
        "verdict": "ok" if prices else "fail",
        "prices": prices,
        "failures": failures,
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "hint": ("ทุก call ถูกบันทึกใน log แล้ว — กดรีเฟรชเพื่อดูผลล่าสุด"
                 if prices else "ทุก feed ล้มเหลว — ดูรายละเอียด error ใน log"),
    }
