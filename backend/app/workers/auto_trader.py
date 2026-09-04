"""Worker #5 — AutoTrader (every 1 min).

Reads order_mode from trading_settings:
    auto      → pick up pending signals, run the full execution gate, fire orders
    semi_auto → do nothing (signals wait for human /approve)
    manual    → do nothing at all

All orders go through app.services.execution.execute_signal — the same path as
/approve — so the pause switch, kill switch, frequency limits, news block,
correlation cap and risk officer apply identically to both paths.
"""
from __future__ import annotations

import logging

from app.models.schemas import AppSettings
from app.services import execution, signal_log
from app.services.execution import SIGNAL_TTL_MIN, expire_stale_pending_signals, now_iso

log = logging.getLogger(__name__)


async def trade_once(db, broker, notifier) -> dict:
    """One auto-trader cycle. Returns a small summary for logs/tests."""
    s: AppSettings = execution.get_app_settings(db)
    if s.order_mode != "auto":
        # Still expire stale pending signals so the signals page never shows
        # dead entries — expiry is not an auto-mode-only concern.
        expired = expire_stale_pending_signals(db)
        return {"mode": s.order_mode, "picked": 0, "fired": 0, "expired": expired}

    expired = expire_stale_pending_signals(db)
    pending = db.select("signals", filters={"approval": "pending"}, limit=10)
    # Defense-in-depth for the duplicate-position loop (2026-09-04): never
    # stack a second position on an asset that already has one open. The
    # scanner now dedups too, but this gate is the last line before an order
    # leaves the platform.
    open_assets = {
        str(r.get("asset") or "").upper()
        for r in db.select("paper_trades", filters={"status": "open"}, limit=200)
    }
    fired, blocked, skipped = 0, 0, 0
    for sig in pending:
        entry = float(sig.get("entry") or 0)
        if entry <= 0:
            signal_log.log_event(
                db=db, event="order_blocked", signal_id=str(sig.get("id") or ""),
                asset=str(sig.get("asset") or ""),
                direction=str(sig.get("direction") or ""),
                confidence=sig.get("confidence"), entry=sig.get("entry"),
                source="auto", reason="entry ไม่ถูกต้อง (0) — ไม่เปิดออเดอร์")
            continue
        if str(sig.get("asset") or "").upper() in open_assets:
            skipped += 1
            log.info("AutoTrader skipped %s %s: position already open",
                     sig["direction"], sig["asset"])
            signal_log.log_event(
                db=db, event="order_blocked", signal_id=str(sig.get("id") or ""),
                asset=str(sig.get("asset") or ""),
                direction=str(sig.get("direction") or ""),
                confidence=sig.get("confidence"), entry=sig.get("entry"),
                source="auto",
                reason=f"{sig.get('asset')} มีไม้เปิดอยู่แล้ว — รอปิดไม้เดิมก่อน")
            continue
        report = await execution.execute_signal(
            db, broker, notifier, s,
            user_id=sig.get("user_id", execution.DEFAULT_USER),
            asset=sig["asset"], direction=str(sig["direction"]).upper(),
            entry=entry, stop_loss=sig.get("stop_loss"),
            take_profit=sig.get("take_profit"),
            confidence=float(sig.get("confidence") or 0),
            opportunity=float(sig.get("opportunity_score") or sig.get("confidence") or 0),
            signal_id=sig.get("id"), source="auto",
        )
        if report.allowed:
            db.update("signals", sig["id"], {"approval": "approved"})
            # Separate call — tolerates a DB without the 010 approved_at column.
            db.update("signals", sig["id"], {"approved_at": now_iso()})
            fired += 1
        else:
            blocked += 1
            log.info("AutoTrader blocked %s %s: %s",
                     sig["direction"], sig["asset"], report.rejects[:1])

    return {"mode": "auto", "picked": len(pending), "fired": fired,
            "blocked": blocked, "skipped": skipped, "expired": expired}
