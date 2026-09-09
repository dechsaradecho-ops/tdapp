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
    # Per-symbol spread overrides: an empty dict ({} from the Settings page
    # when every override was cleared) is normalized to None so the JSONB
    # column stores NULL → built-in DEFAULT_SPREADS apply. Non-empty dicts
    # pass through (unknown assets are harmless — the resolver ignores them).
    if "spread_overrides" in (payload or {}) \
            and not (payload or {}).get("spread_overrides"):
        patch["spread_overrides"] = None
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
        # Capital regime change → reseed equity history. A 10k→100 change
        # leaves 10k-era snapshot peaks that fake 99% drawdown until every
        # old row ages out (prod 2026-09-09). Same rule as the 3x clamp in
        # equity_drawdown_pct, but permanent instead of per-read.
        try:
            old_cap = float(getattr(current, "capital", 0) or 0)
            new_cap = float(getattr(merged, "capital", 0) or 0)
            if old_cap > 0 and new_cap > 0 and abs(new_cap - old_cap) / old_cap > 0.5:
                from datetime import timezone as _tz
                for r in db.select("equity_snapshots", limit=500) or []:
                    try:
                        db.delete("equity_snapshots", {"id": r.get("id")})
                    except Exception:
                        pass
                db.insert("equity_snapshots", {
                    "user_id": "demo",
                    "snapshot_date": datetime.now(_tz.utc).date().isoformat(),
                    "equity": round(new_cap, 2),
                })
        except Exception as exc:
            log.warning("equity reseed on capital change failed: %s", exc)
        return SettingsSaveResult(
            ok=True, settings=_row_to_settings(resp.data[0]),
            message="saved")
    except Exception as exc:
        log.error("save app_settings failed: %s", exc)
        # PGRST204 = PostgREST schema cache miss — almost always a missing
        # column from a migration that hasn't been applied yet. Retry the
        # upsert WITHOUT the unknown column(s) so the other settings still
        # land (e.g. min_confidence_gold used to fail to save entirely once
        # newer columns like sl_distance_* had no migration applied yet).
        raw = str(exc)
        if "PGRST204" in raw:
            import re
            m = (re.search(r"'([^']+)'\s+of schema", raw)
                 or re.search(r"Could not find the '([^']+)' column", raw))
            missing = m.group(1) if m else None
            if missing and missing in row:
                row.pop(missing, None)
                try:
                    resp = db._client.table(SETTINGS_TABLE).upsert(row).execute()
                    if resp.data:
                        return SettingsSaveResult(
                            ok=True, settings=_row_to_settings(resp.data[0]),
                            message=(f"saved (column '{missing}' skipped — "
                                     "รัน migration ที่เกี่ยวข้องใน Supabase "
                                     "SQL Editor เพื่อเปิดใช้ฟีเจอร์นี้)"))
                except Exception as exc2:
                    log.error("save app_settings retry without %s failed: %s",
                              missing, exc2)
        if "PGRST204" in raw and "allowed_assets" in raw:
            return SettingsSaveResult(
                ok=False, settings=merged,
                message="ยังไม่มี column allowed_assets ใน Supabase — รัน database/021_allowed_assets.sql ใน SQL Editor ก่อน")
        return SettingsSaveResult(ok=False, settings=merged, message=raw)


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
