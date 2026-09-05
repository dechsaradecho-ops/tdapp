"""Worker #3 — Portfolio Monitor (every 1 min).

Computes current drawdown, open risk and exposure from the paper_trades
journal. On limit breach:
  1. engages the trading pause (execution.set_pause — the SAME switch the
     gate reads, so the next order is blocked immediately)
  2. sends the risk alert through NotificationService (risk_warning is a
     CRITICAL type → pushed to LINE instantly)
  3. logs a risk_events row for the audit trail

Also writes one equity_snapshots row per cycle (deduped per UTC day) — the
equity curve that powers the REAL drawdown in the kill switch and the
performance page chart.

BUG FIXED (2026-09-05): the breach path built the alert but never called
notifier.notify nor set_pause — breaches were logged and silently ignored.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.engine.risk_engine import PortfolioSnapshot, RiskEngine
from app.integrations.line_client import build_risk_alert
from app.services import execution
from app.services.database import Database
from app.services.notification_service import NotificationService

log = logging.getLogger(__name__)


def _realized_pnl_since(closed_trades: list[dict], since: datetime) -> float:
    """Sum closed-trade PnL whose closed_at is within the window."""
    total = 0.0
    for t in closed_trades:
        raw = t.get("closed_at")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= since:
            total += float(t.get("pnl") or 0)
    return total


def _equity(db, capital: float) -> float:
    """Current equity = settings capital + realized PnL of closed paper trades."""
    try:
        rows = db.select("paper_trades", filters={"status": "closed"}, limit=500)
    except Exception:
        rows = []
    realized = sum(float(r.get("pnl") or 0) for r in rows)
    return capital + realized


def _write_equity_snapshot(db, user_id: str, equity: float) -> bool:
    """One equity_snapshots row per UTC day (dedup by snapshot_date).

    Returns True when a NEW row was written this cycle. Never raises — the
    snapshot is an enhancement, not a dependency.
    """
    if not db or not getattr(db, "available", False):
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        existing = db.select("equity_snapshots",
                             filters={"snapshot_date": today}, limit=1)
        if existing:
            # same-day row: refresh the equity value (intraday drift)
            db.update("equity_snapshots", existing[0]["id"],
                      {"equity": round(equity, 2)})
            return False
        db.insert("equity_snapshots", {
            "user_id": user_id, "snapshot_date": today,
            "equity": round(equity, 2),
        })
        return True
    except Exception as exc:
        log.debug("equity snapshot write failed: %s", exc)
        return False


def monitor_once(db: Database, broker, notifier: NotificationService) -> dict:
    """Evaluate the portfolio against the Risk Engine; act on breaches."""
    s = execution.get_app_settings(db)
    capital = s.capital
    user_id = execution.DEFAULT_USER

    equity = _equity(db, capital)
    _write_equity_snapshot(db, user_id, equity)

    try:
        trades = db.select("paper_trades", limit=500)
    except Exception:
        trades = []
    closed = [t for t in trades if t.get("status") == "closed"]
    open_rows = [t for t in trades if t.get("status") == "open"]

    realized_month = sum(float(t.get("pnl") or 0) for t in closed)
    open_risk = 0.0
    for t in open_rows:
        if t.get("stop_loss") and t.get("entry_price"):
            open_risk += abs(float(t["entry_price"]) - float(t["stop_loss"])) \
                * float(t.get("volume") or 1)

    now = datetime.now(timezone.utc)
    snap = PortfolioSnapshot(
        starting_capital=capital,
        peak_equity=max(capital, equity),
        current_equity=equity,
        realized_pnl_today=_realized_pnl_since(closed, now - timedelta(days=1)),
        realized_pnl_week=_realized_pnl_since(closed, now - timedelta(days=7)),
        realized_pnl_month=realized_month,
        open_risk=open_risk,
    )
    status = RiskEngine().check(snap)

    if status.trading_paused:
        db.insert("risk_events", {
            "user_id": user_id, "event_type": "limit_breach",
            "detail": status.model_dump(),
        })
        # 1) engage the SAME pause switch the execution gate reads — without
        # this the breach was cosmetic and orders kept firing.
        pause = execution.set_pause(
            db, True,
            f"risk engine: {status.message[:180]}")
        # 2) critical alert → NotificationService pushes to LINE immediately
        # (risk_warning ∈ CRITICAL_TYPES) and queues a row for the log.
        alert = build_risk_alert(
            status.current_drawdown_pct, status.max_drawdown_pct,
            "TRADING PAUSED — MANUAL REVIEW REQUIRED. ลดขนาดโพซิชัน/ปิดบางส่วน.",
        )
        try:
            import asyncio
            coro = notifier.notify(user_id, "risk_warning", alert)
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is None:
                asyncio.run(coro)  # sync scheduler thread (to_thread)
            else:
                # in-app scheduler: fire-and-forget on the running loop
                task = running.create_task(coro)
                task.add_done_callback(
                    lambda t: t.exception() and log.error(
                        "risk alert notify failed: %s", t.exception()))
        except Exception as exc:
            log.error("risk alert notify failed: %s", exc)
        log.warning("portfolio monitor: limit breach → trading PAUSED (%s)",
                    status.message[:200])
        return {"checked": 1, "breach": True, "paused": pause.paused,
                "equity": round(equity, 2)}

    return {"checked": 1, "breach": False, "equity": round(equity, 2)}
