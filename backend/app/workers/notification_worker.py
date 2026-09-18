"""Worker #4 — Notification Service.

Sends pending notifications (LINE alerts, daily/weekly/monthly reports).
Critical alerts are also dispatched immediately by NotificationService.
Each dispatch goes out over BOTH transports — LINE and Web Push (browser
notification tray) — so a phone-only user still gets queued (non-critical)
alerts, which the immediate path never touches.
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
        # TWO transports, independently: LINE AND Web Push. Marking 'sent' when
        # EITHER delivered stops the other one from re-alerting the same event
        # a minute later (duplicate phone notifications for one alert).
        ok_line = await notifier.push_line(n["user_id"], n["message"])
        ok_push = await notifier.push_web(n.get("type", ""), n["message"])
        ok = ok_line or ok_push
        db.update("notifications", n["id"], {
            "status": "sent" if ok else "failed",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        })
        sent += 1 if ok else 0
    return sent


# NOTE: the old send_daily_summaries (portfolios/trades tables) was deleted —
# daily_digest.send_digest_once is the single daily summary path (run_all.py).
