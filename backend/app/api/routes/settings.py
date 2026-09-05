"""Settings endpoints — user-configurable trading configuration.

GET  /api/settings        → effective settings (DB row → schema defaults fallback)
PUT  /api/settings        → save full config (upsert single row id=1)
POST /api/settings/reset  → delete the row (engines fall back to defaults)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request

from app.models.schemas import AppSettings, SettingsSaveResult

log = logging.getLogger("settings")

router = APIRouter()

SETTINGS_TABLE = "trading_settings"

# Fields accepted from the client (mirrors AppSettings model fields)
_FIELDS = set(AppSettings.model_fields.keys())


def _row_to_settings(row: Optional[dict[str, Any]]) -> AppSettings:
    if not row:
        return AppSettings()
    fields = set(AppSettings.model_fields.keys())
    clean = {k: v for k, v in row.items() if k in fields and v is not None}
    return AppSettings(**clean)


def _load_settings(db) -> AppSettings:
    if not db or not db.available:
        return AppSettings()
    try:
        client = db._client  # single row by primary key — bypass order-by requirement
        resp = client.table(SETTINGS_TABLE).select("*").eq("id", 1).limit(1).execute()
        rows = list(resp.data or [])
        return _row_to_settings(rows[0] if rows else None)
    except Exception as exc:
        log.error("load app_settings failed: %s", exc)
        return AppSettings()


def get_app_settings(db) -> AppSettings:
    """Shared loader used by other routers (trading.py etc.)."""
    return _load_settings(db)


@router.get("", response_model=AppSettings)
def get_settings(request: Request) -> AppSettings:
    """GET /api/settings — effective configuration (DB → defaults fallback)."""
    return _load_settings(request.app.state.db)


@router.put("", response_model=SettingsSaveResult)
def save_settings(request: Request, payload: dict[str, Any]) -> SettingsSaveResult:
    """PUT /api/settings — merge-persist to the single app_settings row."""
    db = request.app.state.db
    current = _load_settings(db)
    patch = {k: v for k, v in (payload or {}).items() if k in _FIELDS and v is not None}
    # Optional per-asset overrides: an explicit null CLEARS the override so the
    # engine falls back to the base field (the Settings page "ล้าง" button).
    # Absent keys keep the stored value (merge semantics).
    for opt in ("min_confidence_gold", "min_lot_gold"):
        if opt in (payload or {}) and (payload or {})[opt] is None:
            patch[opt] = None
    # model_validate (NOT model_copy) so client values are coerced to field types —
    # e.g. float 30.0 → int 30; Postgres integer columns reject "30.0" (22P02)
    merged = AppSettings.model_validate({**current.model_dump(), **patch})

    if not db or not db.available:
        return SettingsSaveResult(
            ok=False, settings=merged,
            message="DB unavailable — settings not saved")

    try:
        row = merged.model_dump(mode="json")
        row["id"] = 1
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        resp = db._client.table(SETTINGS_TABLE).upsert(row).execute()
        if not resp.data:
            return SettingsSaveResult(ok=False, settings=merged,
                                      message="upsert returned no data")
        return SettingsSaveResult(
            ok=True, settings=_row_to_settings(resp.data[0]),
            message="saved")
    except Exception as exc:
        log.error("save app_settings failed: %s", exc)
        return SettingsSaveResult(ok=False, settings=merged, message=str(exc))


@router.post("/reset", response_model=SettingsSaveResult)
def reset_settings(request: Request) -> SettingsSaveResult:
    """POST /api/settings/reset — drop the row; engines revert to defaults."""
    db = request.app.state.db
    try:
        if db and db.available:
            db._client.table(SETTINGS_TABLE).delete().eq("id", 1).execute()
    except Exception as exc:
        log.error("reset app_settings failed: %s", exc)
    return SettingsSaveResult(ok=True, settings=AppSettings(), message="reset to defaults")
