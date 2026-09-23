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
from app.integrations.line_client import (DRAWDOWN_APPROACH_COOLDOWN_MIN,
                                          build_drawdown_approach_alert,
                                          build_risk_alert)
from app.models.schemas import contract_value_for, risk_usd_of_distance
from app.services import execution, limit_expand
from app.services.database import Database
from app.services.notification_service import NotificationService

log = logging.getLogger(__name__)

# Drawdown at/above this fraction of the kill-switch limit triggers the EARLY
# LINE warning ("ใกล้ถึงเพดาน") while trading is still running. 0.8 matches
# goal_engine.DRAWDOWN_PRESSURE_RATIO, so the goal assessment and the alert
# agree on when a drawdown counts as "eating the budget".
DRAWDOWN_APPROACH_RATIO = 0.8

# Notification type for the early warning. It is NOT "risk_warning": that type
# is a CRITICAL push with a 30-min cooldown and its own wording ("TRADING
# PAUSED"), and reusing it would (a) claim a pause that has not happened and
# (b) let the early warning swallow the real breach alert's cooldown slot.
# "drawdown_warning" is queued (non-critical) → worker #4 delivers it, and it
# is gated by the SAME Settings category as risk_warning (see
# notification_service.NOTIFY_CATEGORY_FIELDS).
DRAWDOWN_WARNING_TYPE = "drawdown_warning"


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
                    # Quote→USD rates: prefer the map the guard cached on the
                    # broker this cycle; empty means PaperBrokerPnl.compute
                    # returns None (fail-closed) and the position is skipped
                    # rather than contributing a wrong-currency figure.
                    _rates = getattr(broker, "_rates", None) or {}
                    v = PaperBrokerPnl.compute(
                        pos, asset=str(getattr(pos, "asset", "") or ""),
                        rates=_rates)
                    if v is not None:
                        unrealized += float(v)
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


def _drawdown_warning_due(db, cooldown_min: float) -> bool:
    """True when the early drawdown warning may be pushed again.

    Reads the newest ``drawdown_warning`` notifications row (created_at is
    stamped by queue_notification). Fail-OPEN: when the lookup breaks we send —
    a repeated warning beats a silently swallowed one, and the alert is
    informational (nothing is paused by it).
    """
    try:
        rows = db.select("notifications",
                         filters={"type": DRAWDOWN_WARNING_TYPE},
                         order="created_at", desc=True, limit=1)
    except Exception:
        return True
    if not rows:
        return True
    raw = str(rows[0].get("created_at") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    return age_min >= cooldown_min


def _notify_drawdown_approach(db, notifier, user_id: str, status,
                              equity: float, peak: float,
                              open_positions: int, open_risk_pct: float) -> bool:
    """Push the "drawdown ใกล้ถึงเพดาน" early warning (throttled). Never raises.

    Fires while the account is STILL TRADING (the breach path below owns the
    "already paused" case), so the owner gets a chance to act before the kill
    switch stops everything. Returns True when a push was attempted.
    """
    try:
        max_dd = float(getattr(status, "max_drawdown_pct", 0) or 0)
        dd = float(getattr(status, "current_drawdown_pct", 0) or 0)
        if max_dd <= 0 or dd < max_dd * DRAWDOWN_APPROACH_RATIO:
            return False
        if not _drawdown_warning_due(db, DRAWDOWN_APPROACH_COOLDOWN_MIN):
            log.info("drawdown approach warning skipped (cooldown %.0f min)",
                     DRAWDOWN_APPROACH_COOLDOWN_MIN)
            return False
        alert = build_drawdown_approach_alert(
            dd, max_dd, max(0.0, max_dd - dd), equity=equity,
            peak_equity=peak, open_positions=open_positions,
            open_risk_pct=open_risk_pct, warn_ratio=DRAWDOWN_APPROACH_RATIO)
        _dispatch_notify(notifier, user_id, DRAWDOWN_WARNING_TYPE, alert)
        log.warning("portfolio monitor: drawdown %.2f%% is %.0f%% of the %.2f%% "
                    "limit → early warning pushed", dd, dd / max_dd * 100, max_dd)
        return True
    except Exception as exc:
        log.error("drawdown approach warning failed: %s", exc)
        return False


def _dispatch_notify(notifier, user_id: str, ntype: str, message: str) -> None:
    """Send through NotificationService from a sync OR async caller.

    Same loop handling as the breach path: scheduler thread → asyncio.run,
    running loop → fire-and-forget task. Never raises — a failed alert must
    not take down the worker that is protecting the account.
    """
    if notifier is None:
        return
    try:
        import asyncio
        coro = notifier.notify(user_id, ntype, message)
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
                    "%s notify failed: %s", ntype, t.exception()))
    except Exception as exc:
        log.error("%s notify failed: %s", ntype, exc)


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
    # math as the heat gate / chat context; the old lots-only sum understated
    # gold risk 100× (XAUUSD contract 100 oz vs FX 100k units).
    #
    # The product (dist × lots × contract) is in the QUOTE currency, so it
    # must be converted to USD: a USDJPY leg's nominal figure is in yen and
    # without the conversion it reads as ~157× its real dollar risk, blowing
    # open_risk_pct past the daily limit and raising a FALSE "TRADING PAUSED"
    # / drawdown-approach alert (same class as the 2026-09-22 SL-cap fix).
    open_risk = 0.0
    for t in open_rows:
        if t.get("stop_loss") and t.get("entry_price"):
            try:
                _asset = str(t.get("asset") or "")
                _entry = float(t["entry_price"])
                _dist = abs(_entry - float(t["stop_loss"]))
                # A fully-closed row can linger as status=open with volume=0
                # (the position guard zeroes it before flipping the status).
                # `volume or 1` turned that 0 into a FULL 1.0 lot, booking
                # ~437 USD of phantom risk on a dead AUDNZD leg and raising a
                # FALSE "TRADING PAUSED" (prod 2026-09-23: open_risk 507.16
                # = 70.16 real + 437.00 phantom). Zero volume = zero risk.
                _lots = float(t.get("volume") or 0)
                if _lots <= 0:
                    continue
                _r = risk_usd_of_distance(_dist, _lots, _asset, _entry)
                if _r is None:
                    _r = _dist * _lots * contract_value_for(_asset)
                open_risk += _r
            except Exception:
                continue

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

    # ---- Shared-definition bridge (prod 2026-09-22) -----------------------
    # ``status`` uses PortfolioSnapshot (this worker's own live broker book),
    # but the position guard's Emergency Exit uses ``execution.evaluate_kill``
    # (equity_snapshots). On 2026-09-22 those two definitions disagreed: the
    # monitor reported "no breach" while the guard was "engaged", so no prompt
    # was ever created and the guard closed 6 positions unprompted. Ask the
    # SAME evaluation the guard uses; if it is engaged, treat it as a breach
    # here too so the prompt is raised by the monitor as well (the guard now
    # also raises one itself — either path guarantees the owner is asked
    # first, and the request is deduped so the owner never sees two).
    kill_breach = False
    kill_triggers: list[str] = []
    try:
        ks = execution.evaluate_kill(db, s)
        kill_breach = bool(getattr(ks, "engaged", False))
        kill_triggers = list(getattr(ks, "triggers", []) or [])
    except Exception as exc:
        log.debug("monitor kill bridge failed: %s", exc)

    if status.trading_paused or kill_breach:
        # The message the pause + audit quote: when the breach came ONLY from
        # the shared kill-switch evaluation (this worker's own snapshot was
        # under the limit) say so, instead of quoting a drawdown that does not
        # look like a breach in the monitor banner.
        if status.trading_paused:
            pause_reason = f"risk engine: {status.message[:180]}"
            alert_text = ("TRADING PAUSED — MANUAL REVIEW REQUIRED. "
                          "ลดขนาดโพซิชัน/ปิดบางส่วน.")
        else:
            pause_reason = ("kill switch: "
                            + ("; ".join(kill_triggers)[:180] or "engaged"))
            alert_text = ("TRADING PAUSED — KILL-SWITCH LIMIT BREACHED. "
                          "รอยืนยันการขยายลิมิตจากเจ้าของ.")
        # audit row ผ่าน limit_expand.write_audit (ไม่กลืน error — เดิม
        # db.insert ลด error เหลือ debug log ทำให้ audit หายเงียบ ๆ)
        limit_expand.write_audit(db, "limit_breach", status.model_dump(),
                                 user_id)
        # 1) engage the SAME pause switch the execution gate reads — without
        # this the breach was cosmetic and orders kept firing.
        pause = execution.set_pause(db, True, pause_reason)
        # 2) critical alert → NotificationService pushes to LINE immediately
        # (risk_warning ∈ CRITICAL_TYPES) and queues a row for the log.
        alert = build_risk_alert(
            status.current_drawdown_pct, status.max_drawdown_pct, alert_text,
        )
        _dispatch_notify(notifier, user_id, "risk_warning", alert)
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
                    pause_reason[:200])
        return {"checked": 1, "breach": True, "paused": pause.paused,
                "equity": round(equity, 2), "kill_bridge": kill_breach}

    # No breach → the account is still trading. Warn EARLY when the drawdown
    # has eaten most of the kill-switch budget ("ถ้ากำลังจะเกิน Max Drawdown
    # ให้ส่ง notification ไปที่ line"): the owner can cut size or close a loser
    # while the platform is still allowed to act, instead of learning about the
    # limit only when trading has already stopped. Throttled per
    # DRAWDOWN_APPROACH_COOLDOWN_MIN so the 1-minute loop cannot spam LINE.
    warned = _notify_drawdown_approach(
        db, notifier, user_id, status, equity=equity,
        peak=snap.peak_equity, open_positions=len(open_rows),
        open_risk_pct=status.open_risk_pct)

    return {"checked": 1, "breach": False, "equity": round(equity, 2),
            "drawdown_warning": warned}
