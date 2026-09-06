"""Central notification service — used by API and workers.

Sends immediately for critical types; other types are persisted for the
Notification Service worker to batch/deliver on schedule.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.integrations.line_client import LineClient
from app.services.database import Database, queue_notification

log = logging.getLogger(__name__)

CRITICAL_TYPES = {"risk_warning", "stop_loss", "economic_news"}

# Cooldown for risk_warning: the portfolio monitor re-evaluates every minute,
# so a standing breach would push an identical LINE alert once a minute.
# One alert per window is enough — the pause stays engaged the whole time.
RISK_WARNING_COOLDOWN_MIN = 30.0


def _parse_utc(raw: str) -> Optional[datetime]:
    """ISO string → tz-aware datetime (None when unparseable)."""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

# User-facing categories (Settings page) → ntype strings they cover.
# Keep in sync with the frontend category list (settings page + types.ts).
NOTIFY_CATEGORY_FIELDS = {
    "notify_trade_opened": {"trade_opened"},
    "notify_trade_closed": {"trade_closed"},
    "notify_stop_loss": {"stop_loss"},
    "notify_risk_warning": {"risk_warning"},
    "notify_daily_digest": {"daily_digest"},
    "notify_daily_summary": {"daily_portfolio_summary", "daily_market_summary",
                             "weekly_report", "monthly_report"},
}


def category_enabled(settings, ntype: str) -> bool:
    """True if the notification category covering `ntype` is enabled.
    Missing settings / unknown type → enabled (default-on behaviour)."""
    if settings is None:
        return True
    for field, types in NOTIFY_CATEGORY_FIELDS.items():
        if ntype in types:
            return bool(getattr(settings, field, True))
    return True


class NotificationService:
    def __init__(self, db: Database, line: LineClient) -> None:
        self.db = db
        self.line = line

    async def notify(self, user_id: str, ntype: str, message: str,
                     critical: bool | None = None) -> None:
        is_critical = critical if critical is not None else ntype in CRITICAL_TYPES
        # Per-category switch: a disabled category produces no queue row and
        # no immediate push (user turned it off from the Settings page).
        try:
            from app.api.routes.settings import get_app_settings
            settings = get_app_settings(self.db)
        except Exception:
            settings = None
        if not category_enabled(settings, ntype):
            log.info("notify skipped (category off): %s", ntype)
            return
        if ntype == "risk_warning" and self._risk_warning_on_cooldown():
            log.info("notify skipped (risk_warning cooldown %.0f min)",
                     RISK_WARNING_COOLDOWN_MIN)
            return
        queue_notification(self.db, user_id, ntype, message)

        if not is_critical:
            return
        await self.push_line(user_id, message)

    def _risk_warning_on_cooldown(self) -> bool:
        """True when a risk_warning row was already queued inside the window.

        Reads the newest notifications row of the type (created_at is stamped
        by queue_notification). Fail-open: when the lookup breaks we send —
        a repeated risk alert beats a silently swallowed one.
        """
        try:
            rows = self.db.select("notifications",
                                  filters={"type": "risk_warning"},
                                  order="created_at", desc=True, limit=1)
        except Exception:
            return False
        if not rows:
            return False
        dt = _parse_utc(str(rows[0].get("created_at") or ""))
        if dt is None:
            return False
        age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        return age_min < RISK_WARNING_COOLDOWN_MIN

    async def push_line(self, user_id: str, message: str) -> bool:
        """Push to every enabled LINE target of the user: personal chats
        (line_users) AND registered groups/rooms (line_targets)."""
        ok = False
        line_users = self.db.select("line_users", filters={"user_id": user_id})
        for lu in line_users:
            if lu.get("notification_enabled"):
                ok = await self.line.push(lu["line_user_id"], message) or ok
        for t in self.db.select("line_targets",
                                filters={"notification_enabled": True}):
            ok = await self.line.push(t["target_id"], message) or ok
        return ok
