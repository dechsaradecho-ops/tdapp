"""Worker #3 — Portfolio Monitor (every 1 min).

Computes current drawdown, open risk and exposure from the paper_trades
journal. On limit breach:
  1. engages the trading pause (execution.set_pause — the SAME switch the
     gate reads, so the next order is blocked immediately)
  2. sends the risk alert through NotificationService (risk_warning is a
     CRITICAL type → pushed to LINE instantly)
  3. logs a risk_events row for the audit trail
  4. asks the OWNER on LINE before any limit may grow (limit_expand): a
     pending request + Approve/Reject buttons — the platform NEVER widens a
     risk limit on its own. Approving updates the limit, lifts the pause and
     re-runs the kill switch (prod 2026-09-14 incident).

Also writes one equity_snapshots row per cycle (deduped per UTC day) — the
equity curve that powers the REAL drawdown in the kill switch and the
performance page chart.

BUG FIXED (2026-09-05): the breach path built the alert but never called
notifier.notify nor set_pause — breaches were logged and silently ignored.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.engine.risk_engine import PortfolioSnapshot, risk_engine_for_settings
from app.integrations.line_client import build_risk_alert
from app.services import execution, limit_expand
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


def _equity(db, capital: float, broker=None) -> float:
    """Current equity = capital + realized PnL + unrealized (broker book).

    The old version ignored open positions, so a floating -8% showed as
    0% drawdown until the trades closed. Unrealized comes from the broker
    book (no extra feed fetch — the guard refreshes book marks every tick;
    stale book just means a slightly stale snapshot, never a crash).
    Never raises.
    """
    try:
        rows = db.select("paper_trades", filters={"status": "closed"}, limit=500)
    except Exception:
        rows = []
    realized = sum(float(r.get("pnl") or 0) for r in rows)
    unrealized = 0.0
    if broker is not None:
        try:
            from app.services.execution import PaperBrokerPnl
            positions = broker.all_positions()
            import inspect as _inspect
            if _inspect.iscoroutine(positions):
                # sync scheduler thread — no running loop here
                import asyncio as _asyncio
                try:
                    _asyncio.get_running_loop()
                    positions = []
                except RuntimeError:
                    positions = _asyncio.run(positions)
            for pos in positions or []:
                try:
                    unrealized += float(PaperBrokerPnl.compute(pos))
                except Exception:
                    continue
        except Exception:
            unrealized = 0.0
    return capital + realized + unrealized


def _peak_equity(db, capital: float, equity: float) -> float:
    """Historical peak equity — thin alias of execution.peak_equity.

    Kept for backward-compat (tests import this name); the SINGLE shared
    definition lives in execution.peak_equity so monitor / chat / kill
    can never drift apart.
    """
    try:
        return execution.peak_equity(db, capital, equity)
    except Exception:
        return max(capital, equity)


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

    # A confirmation window that lapsed is settled BEFORE this cycle judges the
    # account ("ถ้า confirm หมดอายุให้ดำเนินการขยาย limit เลย"): the expansion has
    # to be visible to the risk check below, otherwise the monitor would pause
    # for a limit it is about to widen and un-pause again in the same tick.
    # Only the monitor and the owner's own answer perform this write.
    try:
        settle, _notified = limit_expand.settle_lapsed_window(db, s, notifier,
                                                              user_id)
        if settle.settled and settle.report:
            s = execution.get_app_settings(db)   # widened limits for this cycle
            capital = s.capital
            log.warning("portfolio monitor: settled a lapsed limit-expand "
                        "request (%s) and reported it", settle.kind)
    except Exception as exc:
        log.error("kill expand auto-apply failed: %s", exc)

    equity = _equity(db, capital, broker)
    _write_equity_snapshot(db, user_id, equity)

    try:
        trades = db.select("paper_trades", limit=500)
    except Exception:
        trades = []
    closed = [t for t in trades if t.get("status") == "closed"]
    open_rows = [t for t in trades if t.get("status") == "open"]

    realized_month = sum(float(t.get("pnl") or 0) for t in closed)
    # Open risk in ACCOUNT CURRENCY (contract × lots × SL distance) — same
    # math as chat context; the old lots-only sum understated gold risk 100×
    # (XAUUSD contract 100 oz vs FX 100k units).
    open_risk = 0.0
    for t in open_rows:
        if t.get("stop_loss") and t.get("entry_price"):
            try:
                from app.services.execution import PaperBrokerPnl as _Pnl
                _contract = _Pnl.CONTRACT_SIZES.get(
                    str(t.get("asset") or "").upper(), 100_000.0)
            except Exception:
                _contract = 100_000.0
            open_risk += abs(float(t["entry_price"]) - float(t["stop_loss"])) \
                * float(t.get("volume") or 1) * _contract

    now = datetime.now(timezone.utc)
    snap = PortfolioSnapshot(
        starting_capital=capital,
        peak_equity=_peak_equity(db, capital, equity),
        current_equity=equity,
        realized_pnl_today=_realized_pnl_since(closed, now - timedelta(days=1)),
        realized_pnl_week=_realized_pnl_since(closed, now - timedelta(days=7)),
        realized_pnl_month=realized_month,
        open_risk=open_risk,
        open_positions=len(open_rows),
    )
    # Limits follow the user's Settings row — RiskEngine() alone reads ENV
    # defaults and kept alerting 2% after the user set daily loss to 5%.
    status = risk_engine_for_settings(s).check(snap)

    if status.trading_paused:
        # audit row ผ่าน limit_expand.write_audit (ไม่กลืน error — เดิม
        # db.insert ลด error เหลือ debug log ทำให้ audit หายเงียบ ๆ)
        limit_expand.write_audit(db, "limit_breach", status.model_dump(),
                                 user_id)
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
        # 3) NEVER widen a risk limit silently. A breach only creates a PENDING
        # request and pushes ONE actionable prompt (Approve/Reject quick-reply
        # or /dd_ok, /dd_no); the pause stays engaged until the owner answers.
        # Deduped inside limit_expand (one prompt per window) so this 1-min
        # loop cannot spam the chat with the same question. A lapsed window is
        # settled at the TOP of this cycle (settle_lapsed_window), i.e. before
        # this call, so a lapsed row can never stack up a second one.
        try:
            limit_expand.request_and_notify(db, s, notifier, user_id)
        except Exception as exc:
            log.error("limit expand request failed: %s", exc)
        log.warning("portfolio monitor: limit breach → trading PAUSED (%s)",
                    status.message[:200])
        return {"checked": 1, "breach": True, "paused": pause.paused,
                "equity": round(equity, 2)}

    return {"checked": 1, "breach": False, "equity": round(equity, 2)}
