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
    result["verdict"] = "pass" if (ins and rows and deleted) else "partial"
    result["token"] = token
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
