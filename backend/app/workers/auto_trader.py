"""Worker #5 — AutoTrader (every 1 min).

Reads entry_mode from trading_settings (P1-2; falls back to the legacy
order_mode when entry_mode is unset — see AppSettings.effective_entry_mode):
    auto      → pick up pending signals, run the full execution gate, fire orders
    confirm   → do nothing (signals wait for a human /approve; legacy semi_auto)
    advisory  → do nothing at all (signals are informational; legacy manual)

All orders go through app.services.execution.execute_signal — the same path as
/approve — so the pause switch, kill switch, frequency limits, news block,
correlation cap and risk officer apply identically to both paths.
"""
from __future__ import annotations

import inspect
import logging

from app.api.routes.settings import try_load_settings
from app.models.schemas import AppSettings
from app.services import execution, signal_log
from app.models.schemas import G
from app.services.execution import expire_stale_pending_signals, now_iso

log = logging.getLogger(__name__)


async def trade_once(db, broker, notifier) -> dict:
    """One auto-trader cycle. Returns a small summary for logs/tests."""
    # STRICT read (fail-closed, prod 2026-09-24): an unreadable settings row
    # aborts the cycle — sizing/gates on guessed limits is how sub-floor
    # stops fired while the floor was active. Expiry still runs (harmless
    # cleanup with its own canonical fallback).
    s = try_load_settings(db)
    if s is None:
        log.error("auto-trader: settings unreadable — aborting cycle "
                  "(fail-closed, no guessed limits)")
        expired = expire_stale_pending_signals(db)
        return {"mode": "unknown", "picked": 0, "fired": 0,
                "expired": expired, "aborted": "settings_unreadable"}
    if not s.entry_is_auto():
        # Still expire stale pending signals so the signals page never shows
        # dead entries — expiry is not an auto-mode-only concern.
        expired = expire_stale_pending_signals(db)
        return {"mode": s.effective_entry_mode(), "picked": 0, "fired": 0,
                "expired": expired}

    expired = expire_stale_pending_signals(db)
    pending = db.select("signals", filters={"approval": "pending"},
                        limit=int(G(s, "auto_trader_batch_limit")))
    # Defense-in-depth for the duplicate-position loop (2026-09-04): never
    # stack a second position on an asset that already has one open. The
    # scanner now dedups too, but this gate is the last line before an order
    # leaves the platform.
    #
    # FAIL-CLOSED (2026-09-07): the old read used db.select, which swallows
    # errors and returns [] — a transient Supabase hiccup (prod 2026-09-06
    # 21:25 UTC) made this gate see NO open positions and fired duplicate
    # AUDUSD/XAUUSD orders on top of live ones. Now: (1) a read error aborts
    # the whole cycle — never trade when the safety data is unavailable;
    # (2) the broker's own book (DB-independent, rehydrated at startup) is
    # merged in as a second source of truth.
    try:
        open_assets = {
            str(r.get("asset") or "").upper()
            for r in db.select_ex("paper_trades", filters={"status": "open"},
                                  limit=200)
        }
    except Exception as exc:
        log.error("auto-trader: cannot read open positions (%s) — aborting "
                  "cycle, %d pending signal(s) untouched", exc, len(pending))
        for sig in pending:
            signal_log.log_event(
                db=db, event="order_blocked", signal_id=str(sig.get("id") or ""),
                asset=str(sig.get("asset") or ""),
                direction=str(sig.get("direction") or ""),
                confidence=sig.get("confidence"), entry=sig.get("entry"),
                source="auto",
                reason="อ่านสถานะไม้เปิดไม่สำเร็จ — งดเทรดรอบนี้ (fail-safe กันเปิดซ้ำ)")
        return {"mode": "auto", "picked": len(pending), "fired": 0,
                "blocked": 0, "skipped": 0, "expired": expired,
                "aborted": "open_positions_unreadable"}
    broker_assets: set[str] = set()
    try:
        if broker is not None and hasattr(broker, "all_positions"):
            positions = broker.all_positions()
            if inspect.iscoroutine(positions):
                positions = await positions
            broker_assets = {
                str(getattr(p, "asset", "") or "").upper() for p in positions}
    except Exception as exc:
        log.warning("auto-trader: broker book read failed (%s) — the DB "
                    "layer still guards the gate", exc)
    open_assets |= broker_assets
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
            signal_row=sig,
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
