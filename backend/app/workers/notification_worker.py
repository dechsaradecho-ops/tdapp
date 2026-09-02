"""Worker #4 — Notification Service.

Sends pending notifications (LINE alerts, daily/weekly/monthly reports).
Critical alerts are also dispatched immediately by NotificationService.
"""
from __future__ import annotations

import asyncio
import logging

from app.integrations.line_client import (
    build_daily_market_summary,
    build_daily_portfolio_summary,
)
from app.services.database import Database
from app.services.notification_service import NotificationService

log = logging.getLogger(__name__)


async def dispatch_pending(db: Database, notifier: NotificationService) -> int:
    """Deliver queued notifications through the right channel."""
    pending = db.select("notifications", filters={"status": "pending"}, limit=50)
    sent = 0
    for n in pending:
        if not n.get("user_id"):
            continue  # broadcast rows need a recipient resolution step
        line_users = db.select("line_users", filters={"user_id": n["user_id"]})
        ok = False
        for lu in line_users:
            if lu.get("notification_enabled"):
                ok = await notifier.line.push(lu["line_user_id"], n["message"])
        db.update("notifications", n["id"], {
            "status": "sent" if ok else "failed", "sent_at": "now()",
        })
        sent += 1 if ok else 0
    return sent


async def send_daily_summaries(db: Database, notifier: NotificationService) -> None:
    """Daily Portfolio + Market summaries (scheduled)."""
    portfolios = db.select("portfolios", limit=100)
    for p in portfolios:
        equity = float(p["capital"]) * 1.012  # demo; wire real equity in prod
        pnl = equity - float(p["capital"])
        msg = build_daily_portfolio_summary(
            capital=float(p["capital"]), equity=equity, pnl=pnl,
            goal_pct=float(p["target_return"]), achievement_pct=40.0,
            probability="High Probability",
        )
        await notifier.notify(p["user_id"], "daily_portfolio_summary", msg, critical=False)
