"""Lightweight data-access service over Supabase (service-role on the backend).

All methods are defensive: if Supabase credentials are missing (local dev without
a project), calls degrade gracefully instead of crashing workers/API.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import get_settings

log = logging.getLogger(__name__)


class Database:
    def __init__(self) -> None:
        s = get_settings()
        self._client = None
        self.init_error: Optional[str] = None
        if not s.supabase_url and not s.effective_service_key:
            self.init_error = "env missing: SUPABASE_URL and SUPABASE_SECRET_KEY are both unset"
        elif not s.supabase_url:
            self.init_error = "env missing: SUPABASE_URL is unset"
        elif not s.effective_service_key:
            self.init_error = "env missing: SUPABASE_SECRET_KEY (or legacy SUPABASE_SERVICE_KEY) is unset"
        else:
            try:
                from supabase import create_client
                self._client = create_client(s.supabase_url, s.effective_service_key)
            except Exception as exc:  # pragma: no cover
                self.init_error = f"create_client failed: {exc}"
                log.warning("Supabase init failed (%s) — running without DB", exc)

    @property
    def available(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    def insert(self, table: str, row: dict[str, Any]) -> Optional[dict]:
        if not self._client:
            log.debug("DB unavailable — skip insert into %s", table)
            return None
        try:
            resp = self._client.table(table).insert(row).execute()
            return resp.data[0] if resp.data else None
        except Exception as exc:
            log.error("insert %s failed: %s", table, exc)
            return None

    def insert_raw(self, table: str,
                   row: dict[str, Any]) -> tuple[Optional[dict], Optional[str]]:
        """Insert that returns the RAW error string instead of swallowing it.

        Used by the /api/system/db-check probe to surface RLS/policy/schema
        errors that `insert` normally reduces to a log line.
        """
        if not self._client:
            return None, "client unavailable"
        try:
            resp = self._client.table(table).insert(row).execute()
            return (resp.data[0] if resp.data else None), None
        except Exception as exc:
            return None, str(exc)

    def select(self, table: str, filters: Optional[dict] = None,
               order: str = "created_at", desc: bool = True, limit: int = 50) -> list[dict]:
        if not self._client:
            return []
        try:
            q = self._client.table(table).select("*")
            for col, val in (filters or {}).items():
                q = q.eq(col, val)
            q = q.order(order, desc=desc).limit(limit)
            return list(q.execute().data or [])
        except Exception as exc:
            log.error("select %s failed: %s", table, exc)
            return []

    def update(self, table: str, row_id: str, changes: dict[str, Any]) -> bool:
        if not self._client:
            return False
        try:
            self._client.table(table).update(changes).eq("id", row_id).execute()
            return True
        except Exception as exc:
            log.error("update %s/%s failed: %s", table, row_id, exc)
            return False

    def delete(self, table: str, filters: dict[str, Any]) -> bool:
        """Delete rows matching simple equality filters (probe cleanup)."""
        if not self._client:
            return False
        try:
            q = self._client.table(table).delete()
            for col, val in filters.items():
                q = q.eq(col, val)
            q.execute()
            return True
        except Exception as exc:
            log.error("delete %s %s failed: %s", table, filters, exc)
            return False


def queue_notification(db: Database, user_id: str, ntype: str, message: str,
                       channel: str = "line") -> None:
    """Persist a notification row (worker #4 picks up pending rows)."""
    db.insert("notifications", {
        "user_id": user_id, "channel": channel, "type": ntype,
        "message": message, "status": "pending",
    })
