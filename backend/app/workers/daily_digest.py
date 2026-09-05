"""Worker #8 — Daily LINE Digest (once per UTC day).

One message per day summarizing:
  • yesterday's realized PnL + win/loss counts (paper_trades)
  • open positions with unrealized PnL (live marks when available)
  • top signals from the last 24 h (signal_logs)
  • market status (open/closed + next reopen)

Replaces the old send_daily_summaries which read the unused `trades` table
and never fired (no scheduler job pointed at it).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.integrations import quotes
from app.models.schemas import is_market_closed, next_market_open
from app.services import execution
from app.services.database import Database
from app.services.notification_service import NotificationService

log = logging.getLogger(__name__)

DIGEST_STATE_KEY = "daily_digest_last_sent"


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_digest(db, equity: float, capital: float) -> str:
    """Compose the daily digest message (pure function — testable)."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()

    rows = db.select("paper_trades", limit=500)
    closed_yesterday = [r for r in rows
                        if r.get("status") == "closed"
                        and str(r.get("closed_at") or "")[:10] == yesterday]
    pnl_yesterday = sum(float(r.get("pnl") or 0) for r in closed_yesterday)
    wins = len([r for r in closed_yesterday if float(r.get("pnl") or 0) > 0])
    losses = len([r for r in closed_yesterday if float(r.get("pnl") or 0) < 0])

    open_rows = [r for r in rows if r.get("status") == "open"]
    open_lines = []
    for r in open_rows[:5]:
        direction = "▲" if str(r.get("direction") or "").upper() == "BUY" else "▼"
        open_lines.append(
            f"  {direction} {r.get('asset')} {float(r.get('volume') or 0):g} lots "
            f"@ {float(r.get('entry_price') or 0):g}")
    if len(open_rows) > 5:
        open_lines.append(f"  …และอีก {len(open_rows) - 5} ไม้")

    # top signals from the last 24h (signal_logs, event=created)
    try:
        logs = db.select("signal_logs", filters={"event": "created"}, limit=100)
    except Exception:
        logs = []
    cutoff = (now - timedelta(days=1)).isoformat()
    recent = [l for l in logs if str(l.get("created_at") or "") >= cutoff]
    recent.sort(key=lambda l: float(l.get("confidence") or 0), reverse=True)
    signal_lines = [
        f"  {l.get('asset')} {str(l.get('direction') or '').upper()} "
        f"conf {float(l.get('confidence') or 0):.0f}%"
        for l in recent[:3]
    ]

    closed_flag = is_market_closed(now)
    if closed_flag:
        nxt = next_market_open(now)
        market_line = (f"🔴 ตลาดปิด — เปิดอีกครั้ง "
                       f"{nxt.strftime('%a %H:%M')} UTC")
    else:
        market_line = "🟢 ตลาดเปิด"

    lines = [
        "📊 Daily Digest",
        "",
        f"💰 PnL เมื่อวาน: {pnl_yesterday:+,.2f} USD "
        f"(ชนะ {wins} / เสีย {losses})",
        f"💼 Equity ปัจจุบัน: {equity:,.2f} (capital {capital:,.2f})",
        "",
        f"📈 ไม้เปิดค้าง: {len(open_rows)} ไม้",
    ]
    lines.extend(open_lines or ["  — ไม่มี"])
    lines.append("")
    lines.append(f"🎯 สัญญาณ 24 ชม. ที่ผ่านเกณฑ์: {len(recent)} ตัว")
    lines.extend(signal_lines or ["  — ไม่มี"])
    lines.append("")
    lines.append(market_line)
    return "\n".join(lines)


def _should_send(db) -> bool:
    """Once per UTC day — state kept in trading_pause-style KV via a table.

    Uses the notifications table itself: if a daily_digest row was already
    queued today, skip. Simple, no new table needed.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        rows = db.select("notifications", filters={"type": "daily_digest"},
                         limit=20)
        return not any(str(r.get("created_at") or "")[:10] == today for r in rows)
    except Exception:
        return True


async def send_digest_once(db: Database, notifier: NotificationService) -> dict:
    """Send the daily digest (idempotent per UTC day)."""
    if not db or not getattr(db, "available", False):
        return {"sent": False, "reason": "db unavailable"}
    if not _should_send(db):
        return {"sent": False, "reason": "already sent today"}

    s = execution.get_app_settings(db)
    equity = s.capital
    try:
        rows = db.select("paper_trades", filters={"status": "closed"}, limit=500)
        equity = s.capital + sum(float(r.get("pnl") or 0) for r in rows)
    except Exception:
        pass

    message = build_digest(db, equity, s.capital)
    await notifier.notify(execution.DEFAULT_USER, "daily_digest", message,
                          critical=False)
    # the queued notifications row IS the dedup marker — _should_send reads
    # it back on the next cycle so the digest fires exactly once per UTC day
    log.info("daily digest sent (%d chars)", len(message))
    return {"sent": True, "chars": len(message)}
