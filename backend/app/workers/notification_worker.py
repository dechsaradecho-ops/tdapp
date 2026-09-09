"""Worker #4 — Notification Service.

Sends pending notifications (LINE alerts, daily/weekly/monthly reports).
Critical alerts are also dispatched immediately by NotificationService.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.services.database import Database
from app.services.notification_service import NotificationService, category_enabled
from app.api.routes.settings import get_app_settings

log = logging.getLogger(__name__)


async def dispatch_pending(db: Database, notifier: NotificationService) -> int:
    """Deliver queued notifications through the right channel."""
    pending = db.select("notifications", filters={"status": "pending"}, limit=50)
    sent = 0
    # Load settings once per dispatch — category switches may have changed
    # since the rows were queued.
    try:
        settings = get_app_settings(db)
    except Exception:
        settings = None
    for n in pending:
        # NOTE: rows with a NULL user_id are delivered, not skipped — the
        # queue strips user_id when the pseudo-user "demo" fails the uuid
        # cast, and push_line targets every enabled chat anyway.
        # Re-check the category switch at delivery time (it may have been
        # turned off after the row was queued).
        if not category_enabled(settings, n.get("type", "")):
            db.update("notifications", n["id"], {
                "status": "skipped",
                "error": "category disabled",
                "sent_at": datetime.now(timezone.utc).isoformat(),
            })
            continue
        ok = await notifier.push_line(n["user_id"], n["message"])
        db.update("notifications", n["id"], {
            "status": "sent" if ok else "failed",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        })
        sent += 1 if ok else 0
    return sent


# NOTE: the old send_daily_summaries (portfolios/trades tables) was deleted —
# daily_digest.send_digest_once is the single daily summary path (run_all.py).
