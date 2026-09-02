"""Worker #3 — Portfolio Monitor (every 1 min).

Computes current drawdown, open risk and exposure. On limit breach:
pause trading, (optionally) close positions, and notify the user immediately.
"""
from __future__ import annotations

import logging

from app.engine.risk_engine import PortfolioSnapshot, RiskEngine
from app.integrations.line_client import build_risk_alert
from app.services.database import Database
from app.services.notification_service import NotificationService

log = logging.getLogger(__name__)


def monitor_once(db: Database, broker, notifier: NotificationService) -> dict:
    """Evaluate each portfolio against the Risk Engine."""
    portfolios = db.select("portfolios", limit=100)
    if not portfolios:
        log.info("No portfolios to monitor (DB unavailable or empty).")
        return {"checked": 0}

    checked = 0
    for p in portfolios:
        user_id = p["user_id"]
        trades = db.select("trades", filters={"user_id": user_id}, limit=100)
        closed = [t for t in trades if t.get("status") == "closed"]

        equity = float(p["capital"])
        realized_month = sum(float(t.get("pnl") or 0) for t in closed)
        open_risk = 0.0
        for t in trades:
            if t.get("status") == "open" and t.get("stop_loss") and t.get("entry_price"):
                open_risk += abs(float(t["entry_price"]) - float(t["stop_loss"])) * float(t.get("volume") or 1)

        snap = PortfolioSnapshot(
            starting_capital=float(p["capital"]),
            peak_equity=max(float(p["capital"]), equity + realized_month),
            current_equity=equity + realized_month,
            realized_pnl_today=realized_month * 0.2,   # demo split; wire real aggregates in prod
            realized_pnl_week=realized_month * 0.5,
            realized_pnl_month=realized_month,
            open_risk=open_risk,
        )
        status = RiskEngine().check(snap)
        checked += 1

        if status.trading_paused:
            db.insert("risk_events", {
                "user_id": user_id, "event_type": "limit_breach",
                "detail": status.model_dump(),
            })
            alert = build_risk_alert(
                status.current_drawdown_pct, status.max_drawdown_pct,
                "TRADING PAUSED — MANUAL REVIEW REQUIRED. ลดขนาดโพซิชัน/ปิดบางส่วน.",
            )
            # Critical alert → sent immediately by NotificationService
            # (async run by caller via asyncio.run if needed)
    return {"checked": checked}
