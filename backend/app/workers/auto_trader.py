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
from app.services import execution
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
    fired, blocked = 0, 0
    for sig in pending:
        entry = float(sig.get("entry") or 0)
        if entry <= 0:
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
            db.update("signals", sig["id"],
                      {"approval": "approved", "approved_at": now_iso()})
            fired += 1
        else:
            blocked += 1
            log.info("AutoTrader blocked %s %s: %s",
                     sig["direction"], sig["asset"], report.rejects[:1])

    return {"mode": "auto", "picked": len(pending), "fired": fired,
            "blocked": blocked, "expired": expired}
