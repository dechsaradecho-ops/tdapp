"""Central notification service — used by API and workers.

Sends immediately for critical types; other types are persisted for the
Notification Service worker to batch/deliver on schedule.

TWO TRANSPORTS (parallel, independent):
  1. LINE          — line_users + line_targets (the original channel)
  2. Web Push      — push_subscriptions (手機/desktop notification tray)
Both fire for every alert. Neither replaces the other: the LINE bot can be
removed from a group, and a phone can have push permission revoked — one
transport dying must not silence the other. Web Push is silently inert when
the API has no VAPID keys, so nothing changes for LINE-only deployments.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.integrations import web_push
from app.integrations.line_client import LineClient
from app.services.database import Database, queue_notification

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retention — the bell feed keeps 7 days, same window as the other log tables
# ---------------------------------------------------------------------------
# The `notifications` table had NO TTL: every alert ever queued stayed forever
# (prod 2026-09-23: 240 rows and growing). The bell only ever shows the recent
# feed, so older rows are dead weight. 7 days matches quote_api_logs /
# signal_logs / scheduler_runs (log_maintenance purges them all together).
NOTIFICATION_TTL_DAYS = 7
NOTIFICATION_TABLE = "notifications"
PURGE_INTERVAL_S = 3600.0
_last_purge = 0.0


def purge_old_notifications(db: Any, force: bool = False) -> int:
    """Delete notification rows older than 7 days. Throttled unless force=True.

    Returns the number of rows deleted (0 when skipped/unavailable). Prefers
    one bulk delete_before (lt created_at cutoff); falls back to per-row
    deletes when the client lacks delete_before (FakeDatabase). Never raises.
    """
    global _last_purge
    now = time.monotonic()
    if not force and now - _last_purge < PURGE_INTERVAL_S:
        return 0
    _last_purge = now
    try:
        if db is None or not getattr(db, "available", False):
            return 0
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=NOTIFICATION_TTL_DAYS)).isoformat()
        bulk = getattr(db, "delete_before", None)
        if bulk is not None:
            deleted = int(bulk(NOTIFICATION_TABLE, "created_at", cutoff) or 0)
            if deleted:
                log.info("notifications: purged %d rows older than %d days",
                         deleted, NOTIFICATION_TTL_DAYS)
            return deleted
        rows = db.select(NOTIFICATION_TABLE, filters={}, order="created_at",
                         desc=True, limit=1000)
        stale = [r for r in rows if str(r.get("created_at") or "") < cutoff]
        deleted = 0
        for r in stale:
            if not r.get("id"):
                continue
            if db.delete(NOTIFICATION_TABLE, {"id": r["id"]}):
                deleted += 1
        if deleted:
            log.info("notifications: purged %d rows older than %d days",
                     deleted, NOTIFICATION_TTL_DAYS)
        return deleted
    except Exception as exc:
        log.debug("notification purge failed: %s", exc)
        return 0

CRITICAL_TYPES = {"risk_warning", "stop_loss", "economic_news",
                  "trade_opened", "trade_closed",
                  # A trailing/breakeven stop move — stop-related, so it
                  # pushes immediately like stop_loss (and shares its switch).
                  "sl_moved",
                  # Owner-confirmed risk-limit expansion: the Approve/Reject
                  # prompt must NOT be throttled by the risk_warning cooldown,
                  # or the owner could never answer it (prod 2026-09-14).
                  "limit_expand"}

# Cooldown for risk_warning: the portfolio monitor re-evaluates every minute,
# so a standing breach would push an identical LINE alert once a minute.
# One alert per window is enough — the pause stays engaged the whole time.
RISK_WARNING_COOLDOWN_MIN = 30.0

# Cooldown for sl_moved, PER ASSET: the trailing stop ratchets every guard
# cycle, so a trending pair pushed an identical "SL ขยับ" LINE alert every
# ~1 minute (prod 2026-09-23: GBPCHF/EURCHF 7x in a row, 79 same-asset pairs
# < 10 min apart). One alert per asset per window is enough — the stop keeps
# moving silently in between and the final position is always in the journal.
SL_MOVE_COOLDOWN_MIN = 20.0


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
    # sl_moved = a TRAILING/breakeven stop move, NOT a stop-out. It shares the
    # Stop Loss switch on purpose (both are stop-related), but it is a SEPARATE
    # ntype so the "Stop Loss" statistic counts real stop-outs only (prod
    # 2026-09-23: 95 of 119 stop_loss rows were trailing moves) and so it can
    # carry its own per-asset cooldown without throttling real stop-outs.
    "notify_stop_loss": {"stop_loss", "sl_moved"},
    # drawdown_warning = the EARLY "ใกล้ถึงเพดาน Max Drawdown" notice pushed by
    # portfolio_monitor while trading is still running. It shares the ความเสี่ยง
    # switch with risk_warning on purpose: both are drawdown/portfolio-risk
    # alerts, and a user who turned risk alerts off does not want this one
    # either. It is NOT in CRITICAL_TYPES (queued → worker #4 delivers it) and
    # has its own cooldown, so it can never consume the breach alert's slot.
    "notify_risk_warning": {"risk_warning", "drawdown_warning"},
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


# ---------------------------------------------------------------------------
# Web Push (VAPID) presentation helpers
# ---------------------------------------------------------------------------
# The OS notification tray shows a short bold TITLE + a one-line body — a LINE
# message (multi-line, with an emoji header) does not fit. The long text stays
# in LINE and in the dashboard; the phone gets a scannable headline.
PUSH_TITLES = {
    "trade_opened": "เปิดไม้ใหม่",
    "trade_closed": "ปิดไม้แล้ว",
    "stop_loss": "ชน Stop Loss",
    "sl_moved": "ขยับ Stop Loss",
    "risk_warning": "เตือนความเสี่ยง",
    "drawdown_warning": "ใกล้ถึงเพดาน Drawdown",
    "limit_expand": "ขอขยายลิมิตความเสี่ยง",
    "economic_news": "ข่าวเศรษฐกิจ",
    "daily_digest": "สรุปตลาดประจำวัน",
    "daily_portfolio_summary": "สรุปพอร์ตประจำวัน",
    "daily_market_summary": "สรุปตลาดประจำวัน",
    "weekly_report": "รายงานรายสัปดาห์",
    "monthly_report": "รายงานรายเดือน",
}

# Where tapping the notification lands. Static export paths end in .html
# (`/monitor.html` — same targets the /risk and /performance stub pages use).
PUSH_URLS = {
    "economic_news": "/signals.html",
}
DEFAULT_PUSH_URL = "/monitor.html"

# Leading emoji/symbols + whitespace. LINE messages open with "🔔 "/"⚠️ " —
# the UI design system forbids emoji, and the tray already shows an app icon.
_LEAD_SYMBOLS = re.compile(r"^[^0-9A-Za-z\u0E00-\u0E7F]+")


def push_title(ntype: str) -> str:
    return PUSH_TITLES.get(ntype) or "แจ้งเตือนจาก AI Trading"


def push_url(ntype: str) -> str:
    return PUSH_URLS.get(ntype, DEFAULT_PUSH_URL)


def push_body(message: str, limit: int = 140) -> str:
    """First line of the LINE message, emoji stripped, truncated to fit."""
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    first = _LEAD_SYMBOLS.sub("", first).strip()
    return first[:limit]


class NotificationService:
    def __init__(self, db: Database, line: LineClient) -> None:
        self.db = db
        self.line = line

    async def notify(self, user_id: str, ntype: str, message: str,
                     critical: bool | None = None,
                     quick_reply: Optional[list[dict]] = None,
                     asset: Optional[str] = None) -> None:
        """Dispatch one notification.

        ``quick_reply`` attaches LINE postback buttons (used by the
        owner-confirmed limit-expansion prompt: Approve / Reject). It only
        affects the immediate push — the queue row stores the plain text.

        ``asset`` scopes the sl_moved per-asset cooldown (a trailing stop
        ratchets every guard cycle; without it one trending pair spams LINE).
        """
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
        if ntype == "sl_moved" and self._sl_move_on_cooldown(asset):
            log.info("notify skipped (sl_moved cooldown %.0f min, %s)",
                     SL_MOVE_COOLDOWN_MIN, asset)
            return
        # Critical types push immediately; the queue row is stamped 'sent'
        # so worker #4 doesn't re-deliver the same message a minute later.
        # A failed immediate push stays 'pending' — the worker retries it.
        # Non-critical types queue as 'pending' — the worker is the sender.
        if is_critical:
            ok = await self.push_line(user_id, message, quick_reply=quick_reply)
            # Web Push is fired as a SEPARATE transport, not as a fallback: the
            # phone must be alerted even when the LINE group lost the bot.
            pushed = await self.push_web(ntype, message)
            # Record the transport(s) that ACTUALLY delivered — the column used
            # to be hardcoded 'line' even when only Web Push reached the user.
            channel = ("both" if (ok and pushed)
                       else "line" if ok
                       else "web_push" if pushed
                       else "line")
            queue_notification(self.db, user_id, ntype, message,
                               channel=channel,
                               status="sent" if (ok or pushed) else "pending")
        else:
            queue_notification(self.db, user_id, ntype, message)

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

    def _sl_move_on_cooldown(self, asset: Optional[str]) -> bool:
        """True when an sl_moved row for the SAME asset was queued recently.

        The trailing stop ratchets every guard cycle, so without this one
        trending pair pushes an identical "SL ขยับ" alert every ~1 minute
        (prod 2026-09-23). Scoped per asset: a move on GBPCHF must not
        silence a move on EURCHF. Fail-open: a broken lookup sends anyway.
        """
        if not asset:
            return False
        try:
            rows = self.db.select("notifications",
                                  filters={"type": "sl_moved"},
                                  order="created_at", desc=True, limit=20)
        except Exception:
            return False
        needle = f"Asset: {asset}"
        for r in rows or []:
            if needle not in str(r.get("message") or ""):
                continue
            dt = _parse_utc(str(r.get("created_at") or ""))
            if dt is None:
                continue
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
            return age_min < SL_MOVE_COOLDOWN_MIN
        return False

    async def push_line(self, user_id: str, message: str,
                        quick_reply: Optional[list[dict]] = None) -> bool:
        """Push to every enabled LINE target of the user: personal chats
        (line_users) AND registered groups/rooms (line_targets).

        GOTCHA (prod 2026-09-07): user_id is NOT used to filter line_users —
        the column is a uuid FK but the app runs on the pseudo-user "demo",
        so the eq-filter made PostgREST fail (swallowed by select) and
        personal chats never received anything. The app is single-user:
        every enabled chat gets every alert."""
        ok = False

        async def _push(target: str) -> bool:
            # Only pass quick_reply when set so line clients/fakes with the
            # original 2-arg push() signature keep working unchanged.
            if quick_reply:
                return await self.line.push(target, message,
                                            quick_reply=quick_reply)
            return await self.line.push(target, message)

        for lu in self.db.select("line_users",
                                 filters={"notification_enabled": True}):
            ok = await _push(lu["line_user_id"]) or ok
        for t in self.db.select("line_targets",
                                filters={"notification_enabled": True}):
            ok = await _push(t["target_id"]) or ok
        return ok

    async def push_web(self, ntype: str, message: str,
                       url: Optional[str] = None) -> bool:
        """Send the same alert to every device registered for Web Push.

        Not a queue of its own — the orchestrating path (notify for critical,
        dispatch_pending for the rest) already owns persistence. Returns True
        when at least one device accepted the push.

        Never raises: push is a best-effort side channel, and with no VAPID
        keys configured it returns False without touching the network.
        """
        try:
            sent = await web_push.push_all(
                self.db,
                title=push_title(ntype),
                body=push_body(message),
                url=url or push_url(ntype),
                tag=ntype or "tdapp",
                ntype=ntype,
            )
        except Exception as exc:  # web_push is defensive; belt and braces
            log.warning("web push failed (%s): %s", ntype, exc)
            return False
        return sent > 0
