"""Central notification service — used by API and workers.

Sends immediately for critical types; other types are persisted for the
Notification Service worker to batch/deliver on schedule.
"""
from __future__ import annotations

import logging

from app.integrations.line_client import LineClient
from app.services.database import Database, queue_notification

log = logging.getLogger(__name__)

CRITICAL_TYPES = {"risk_warning", "stop_loss", "economic_news"}


class NotificationService:
    def __init__(self, db: Database, line: LineClient) -> None:
        self.db = db
        self.line = line

    async def notify(self, user_id: str, ntype: str, message: str,
                     critical: bool | None = None) -> None:
        is_critical = critical if critical is not None else ntype in CRITICAL_TYPES
        queue_notification(self.db, user_id, ntype, message)

        if not is_critical:
            return
        await self.push_line(user_id, message)

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
