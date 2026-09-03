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

import uuid
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
