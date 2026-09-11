"""Worker #6 — Position Guard (every 1 min).

SL/TP enforcement + position management loop for the PaperBroker:
  1. marks every open position to the latest price (live quote first)
  2. breakeven: profit ≥ breakeven_trigger_r × R → SL moves to entry
  3. trailing: after breakeven, SL trails at trail_atr_mult × ATR
  4. partial close (TP1): profit ≥ partial_trigger_r × R → close
     partial_close_pct of the volume, trail the remainder
  5. closes the position when the stop-loss or take-profit is touched,
     journals the close into paper_trades (so the kill switch / frequency
     engines see realized PnL) and notifies the user.

Real broker adapters (MT5/OANDA) enforce SL/TP server-side; their close events
still flow through close_trade_rows so the journal stays authoritative.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.engine import smart_exit
from app.integrations import quotes
from app.integrations.brokers import Position
from app.services import execution
from app.services import signal_log
from app.services.notification_service import NotificationService

log = logging.getLogger(__name__)

# Wall-clock caps for the three independent per-cycle feeds. They are
# fetched CONCURRENTLY (see guard_once), so a cycle costs ≈ max(caps)
# instead of their sum. Prod 2026-09-11: the same feeds ran SEQUENTIALLY
# (spot ≤20s → snapshots ≤35s → news) which pushed one cycle to ~55s —
# effectively its whole 1-min interval. APScheduler runs the guard with
# max_instances=1, so an overrun made every following tick SKIP: the job
# never completed-and-logged, scheduler_runs stayed empty for
# position_guard, and the Logs page wrongly blamed migration 030.
# The marks cap guards the SL/TP safety path and is never sacrificed to a
# slow snapshot/news feed (each has its own independent cap).
_GUARD_MARKS_BUDGET = 20.0   # live spot marks (safety path)
_GUARD_SNAP_BUDGET = 30.0    # Smart-Exit snapshots (AI score enhancement)
_GUARD_NEWS_BUDGET = 12.0    # Smart-Exit news calendar


async def _none() -> None:
    """Awaitable no-op — keeps asyncio.gather's branches uniform."""
    return None


async def _bounded(coro, budget: float):
    """Await `coro` under its own wall-clock cap; None on timeout/error.

    Never raises: one slow feed degrades one branch of the cycle, it must
    never abort the SL/TP safety path (see _GUARD_*_BUDGET above).
    """
    if budget and budget > 0:
        try:
            return await asyncio.wait_for(coro, timeout=budget)
        except asyncio.TimeoutError:
            log.warning("guard feed exceeded %.0fs — using fallback", budget)
            return None
        except Exception as exc:
            log.debug("guard feed failed: %s", exc)
            return None
    try:
        return await coro
    except Exception as exc:
        log.debug("guard feed failed: %s", exc)
        return None


async def _live_marks(assets: list[str]) -> dict[str, float]:
    """Spot marks from the live quote feed; empty dict on failure (offline-safe).

    The PaperBroker book never ticks on its own — mark_price() returns the
    entry price forever, which made positions whose TP was already breached
    (e.g. GBPUSD TP 1.31286 vs live 1.3536) sit open indefinitely. Live marks
    come first; broker book values are only a fallback for unknown assets.
    """
    try:
        prices, _failures = await quotes.fetch_spot_prices(assets)
        return prices
    except Exception as exc:
        log.warning("live marks unavailable (%s) — falling back to broker book", exc)
        return {}


def _atr_for(pos: Position, fallback_distance: float) -> float:
    """ATR estimate for trailing: 20% of the SL distance (≈ 1.5× ATR tier).

    The guard has no candle history per ticket; the SL distance the signal
    was sized from is a stable proxy (SL = 1.5 × ATR at entry by default).
    """
    if pos.stop_loss is None:
        return fallback_distance * 0.2
    return abs(pos.entry_price - pos.stop_loss) * 0.2


def _r_multiple_at(pos: Position, price: float, db=None) -> float:
    """R-multiple of a position at `price`, measured against the ORIGINAL risk.

    Only used to decide whether the time stop is allowed to cut a position.
    The CURRENT stop is not a valid denominator: breakeven / trailing move it
    to (or past) entry, so `abs(entry - stop_loss)` collapses to ~0 and reports
    a fantasy R. `initial_stop_loss` (migration 021, the same field the monitor
    badge reads) is used first, current SL only as fallback. 0.0 when the risk
    distance is unknown.

    Never raises.
    """
    try:
        entry = float(getattr(pos, "entry_price", 0) or 0)
        price = float(price or 0)
        if entry <= 0 or price <= 0:
            return 0.0
        sl = None
        if db is not None:
            rows = db.select(
                "paper_trades",
                filters={"ticket": str(getattr(pos, "ticket", "") or "")},
                limit=1)
            if rows:
                sl = rows[0].get("initial_stop_loss")
        if sl is None:
            sl = getattr(pos, "stop_loss", None)
        if sl is None:
            return 0.0
        risk = abs(entry - float(sl))
        if risk <= 0:
            return 0.0
        sign = 1 if str(getattr(pos, "direction", "") or "").upper() == "BUY" else -1
        return (price - entry) * sign / risk
    except Exception:
        return 0.0


def _sl_move_kind(pos: Position, db=None) -> str:
    """'breakeven' / 'trailing' when this position's stop was moved, else ''.

    `sl_move_reason` is written by persist_sl_move on EVERY guard move, so
    the journal row is the source of truth. Fallback: when the column is
    empty but `initial_stop_loss` differs from the current SL, the stop WAS
    moved (manual move from the monitor page, or a pre-migration-021 row).

    Used to label the close line: a stop-out at a trailed SL above entry is a
    WIN, and logging it as "ตัดขาดทุน (SL)" made prod read like a loss
    (GBPCHF booked +10.5 with the reason "sl").

    Never raises.
    """
    try:
        ticket = str(getattr(pos, "ticket", "") or "")
        if db is None or not ticket:
            return ""
        rows = db.select("paper_trades", filters={"ticket": ticket}, limit=1)
        if not rows:
            return ""
        kind = str(rows[0].get("sl_move_reason") or "").strip().lower()
        if kind in ("breakeven", "trailing"):
            return kind
        if rows[0].get("sl_moved_at"):
            return "trailing"
        initial = rows[0].get("initial_stop_loss")
        current = getattr(pos, "stop_loss", None)
        if initial is None or current is None:
            return ""
        return "trailing" if abs(float(current) - float(initial)) > 1e-9 else ""
    except Exception:
        return ""


def _position_age_days(pos: Position, db=None) -> float:
    """Age of a position in days — journal created_at first, opened_at fallback.

    The journal row is authoritative (same created_at the monitor's
    exit_info_for reads), so the guard and the monitor can never disagree
    on age. opened_at (restored from created_at by rehydrate_book) is only
    a fallback for rows missing from the DB. Never raises; 0.0 when
    nothing is known (fresh position).
    """
    from datetime import datetime, timezone
    if db is not None:
        try:
            rows = db.select("paper_trades",
                             filters={"ticket": str(getattr(pos, "ticket", "") or "")},
                             limit=1)
            if rows:
                created = rows[0].get("created_at")
                dt = execution._parse_dt(created)
                if dt is not None:
                    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
        except Exception:
            pass
    opened = getattr(pos, "opened_at", None)
    if opened is not None:
        try:
            return max(0.0, (datetime.now(timezone.utc) - opened).total_seconds() / 86400.0)
        except Exception:
            pass
    return 0.0


async def _smart_exit_news(db, s) -> tuple[str, str]:
    """(status, event) for the Smart Exit engine — reuses the entry news gate.

    Returns ("SAFE", "") when the calendar is missing/empty.
    """
    try:
        risk = execution._news_risk(db, s)
        status = str(getattr(risk, "status", "SAFE") or "SAFE").upper()
        nxt = getattr(risk, "next_high_impact", None)
        event = str(getattr(nxt, "event", "") or "") if nxt else ""
        return status, event
    except Exception:
        return "SAFE", ""


def _avg_hold_days(db) -> float:
    """Mean holding time (days) — thin alias of execution.avg_hold_days.

    Kept for backward-compat (tests import this name); the SINGLE shared
    definition lives in execution.avg_hold_days so guard / monitor can
    never drift apart. Never raises.
    """
    try:
        return execution.avg_hold_days(db)
    except Exception:
        return 4.0


async def _apply_smart_exit(db, broker, pos: Position, price: float,
                            decision, s,
                            notifier: NotificationService) -> dict:
    """Execute a Smart Exit recommendation. Returns {"closed", "partial_closed"}.

    MOVE_SL is handled by the legacy breakeven/trailing pass (it owns SL
    persistence); this only executes PARTIAL_25/50 and CLOSE. Never raises.
    """
    out = {"closed": False, "partial_closed": False}
    rec = str(getattr(decision, "recommendation", "HOLD") or "HOLD")
    if rec not in ("PARTIAL_25", "PARTIAL_50", "CLOSE"):
        return out
    ticket = str(getattr(pos, "ticket", "") or "")
    asset = str(getattr(pos, "asset", "") or "")
    direction = str(getattr(pos, "direction", "") or "")
    trigger = str(getattr(decision, "trigger", "") or "")
    why = "; ".join(getattr(decision, "reasoning", []) or [])[:300]
    reason_prefix = f"smart_exit:{trigger}" if trigger else "smart_exit"

    if rec in ("PARTIAL_25", "PARTIAL_50"):
        if getattr(pos, "partial_done", False):
            return out  # TP1 already fired — never scale out twice
        pct = 25.0 if rec == "PARTIAL_25" else 50.0
        slice_vol = round(float(pos.volume or 0) * pct / 100.0, 2)
        if slice_vol <= 0 or slice_vol >= float(pos.volume or 0):
            return out
        try:
            result = await broker.partial_close(ticket, slice_vol)
        except Exception as exc:
            log.warning("smart-exit partial %s failed: %s", ticket, exc)
            return out
        if not getattr(result, "ok", False):
            log.warning("smart-exit partial %s rejected: %s", ticket,
                        getattr(result, "message", ""))
            return out
        out["partial_closed"] = True
        pos.partial_done = True  # type: ignore[attr-defined]
        try:
            row_id = str(getattr(pos, "row_id", "") or "")
            if not row_id:
                rows = db.select("paper_trades", filters={"ticket": ticket}, limit=1)
                row_id = str(rows[0].get("id") or "") if rows else ""
            if row_id:
                db.update("paper_trades", row_id, {"partial_done": True})
        except Exception as exc:
            log.debug("smart-exit partial_done persist failed: %s", exc)
        signal_log.log_event(
            db=db, event="closed", asset=asset, direction=direction,
            entry=pos.entry_price, exit_price=price, ticket=ticket,
            volume=slice_vol, source="auto",
            reason=f"{reason_prefix} แบ่งปิด {slice_vol:g} lots @ {price:g} — {why}")
        try:
            await notifier.notify(
                pos.user_id, "trade_closed",
                f"🧠 Smart Exit ({rec})\nAsset: {asset}\nDirection: {direction}\n"
                f"Closed: {slice_vol:g} lots @ {price:g}\n"
                f"Score: {getattr(decision, 'exit_score', '?')} "
                f"({getattr(decision, 'quality', '?')})\n{why}")
        except Exception as exc:
            log.debug("smart-exit partial notify failed: %s", exc)
        return out

    # rec == "CLOSE"
    try:
        result = await broker.close_position(ticket)
    except Exception as exc:
        log.warning("smart-exit close %s failed: %s", ticket, exc)
        return out
    if not getattr(result, "ok", False):
        log.warning("smart-exit close %s rejected: %s", ticket,
                    getattr(result, "message", ""))
        return out
    out["closed"] = True
    pnl = execution.PaperBrokerPnl.compute(pos, s, asset=asset)
    execution.close_trade_rows(db, ticket, price, pnl, reason_prefix)
    signal_log.log_event(
        db=db, event="closed", asset=asset, direction=direction,
        entry=pos.entry_price, exit_price=price, pnl=pnl, ticket=ticket,
        source="auto",
        reason=f"{reason_prefix} ปิดทั้งไม้ @ {price:g} — {why}")
    try:
        await notifier.notify(
            pos.user_id, "trade_closed",
            f"🧠 Smart Exit (CLOSE)\nAsset: {asset}\nDirection: {direction}\n"
            f"Entry: {pos.entry_price:g} → Exit: {price:g}\n"
            f"Score: {getattr(decision, 'exit_score', '?')} "
            f"({getattr(decision, 'quality', '?')})\nPnL: {pnl:+,.2f}\n{why}")
    except Exception as exc:
        log.error("smart-exit close notify failed: %s", exc)
    return out


async def _manage_position(db, broker, pos: Position, price: float,
                           s, notifier: NotificationService) -> dict:
    """Breakeven / trailing / partial-close pass for ONE position.

    Returns {"moved_sl": bool, "partial_closed": bool, "new_sl": float,
    "old_sl": float, "partial_volume": float} for the summary. Never raises —
    a failed broker call just skips the action this cycle.

    ``old_sl`` is the stop BEFORE the move, so the audit line can say WHICH
    pair moved FROM where to where ("EURCHF@0.93624>0.94337") instead of
    only the destination.
    """
    out = {"moved_sl": False, "partial_closed": False,
           "new_sl": None, "old_sl": None, "partial_volume": None}
    if pos.stop_loss is None or pos.entry_price <= 0:
        return out

    sign = 1 if pos.direction == "BUY" else -1
    r_distance = abs(pos.entry_price - pos.stop_loss)
    if r_distance <= 0:
        return out
    profit_distance = (price - pos.entry_price) * sign  # >0 when winning
    r_multiple = profit_distance / r_distance

    be_trigger = float(getattr(s, "breakeven_trigger_r", 1.0) or 0)
    trail_mult = float(getattr(s, "trail_atr_mult", 2.0) or 0)
    partial_pct = float(getattr(s, "partial_close_pct", 0.0) or 0)
    partial_trigger = float(getattr(s, "partial_trigger_r", 1.0) or 0)

    # ---- 1. partial close (TP1) — once per position -----------------------
    if partial_pct > 0 and partial_trigger > 0 and r_multiple >= partial_trigger \
            and not getattr(pos, "partial_done", False):
        slice_vol = round(pos.volume * partial_pct / 100.0, 2)
        if slice_vol > 0 and slice_vol < pos.volume:
            try:
                result = await broker.partial_close(pos.ticket, slice_vol)
                if result.ok:
                    out["partial_closed"] = True
                    out["partial_volume"] = slice_vol
                    pos.partial_done = True  # type: ignore[attr-defined]
                    # persist the flag — otherwise a restart re-fires TP1
                    try:
                        row_id = str(getattr(pos, "row_id", "") or "")
                        if not row_id:
                            rows = db.select("paper_trades",
                                             filters={"ticket": str(pos.ticket or "")},
                                             limit=1)
                            row_id = str(rows[0].get("id") or "") if rows else ""
                        if row_id:
                            db.update("paper_trades", row_id,
                                      {"partial_done": True})
                    except Exception as exc:
                        log.debug("partial_done persist failed: %s", exc)
                    signal_log.log_event(
                        db=db, event="closed", asset=str(pos.asset or ""),
                        direction=str(pos.direction or ""),
                        entry=pos.entry_price, exit_price=price,
                        ticket=str(pos.ticket or ""), volume=slice_vol,
                        source="auto",
                        reason=f"ปิดบางส่วน (TP1) {slice_vol:g} lots "
                               f"ที่ {price:g} — ที่เหลือ trailing")
                    try:
                        await notifier.notify(
                            pos.user_id, "trade_closed",
                            f"💰 Partial Close (TP1)\n"
                            f"Asset: {pos.asset}\nDirection: {pos.direction}\n"
                            f"Closed: {slice_vol:g} lots @ {price:g}\n"
                            f"Remaining: {pos.volume:g} lots (trailing)")
                    except Exception as exc:
                        log.debug("partial notify failed: %s", exc)
            except Exception as exc:
                log.warning("partial close %s failed: %s", pos.ticket, exc)

    # ---- 2. breakeven + trailing (+ R-ladder floor when enabled) ---------
    new_sl: float | None = None
    if be_trigger > 0 and r_multiple >= be_trigger:
        be_price = pos.entry_price
        if trail_mult > 0:
            atr = _atr_for(pos, r_distance)
            trail_price = price - sign * trail_mult * atr
            # trail only ever TIGHTENS: never below breakeven (BUY) or above
            # it (SELL), and never looser than the current SL.
            if sign == 1:
                new_sl = max(be_price, trail_price)
            else:
                new_sl = min(be_price, trail_price)
            # R-ladder floor (spec priority 4): 1R→BE / 2R→+1R / 3R→+2R.
            # Gated by trailing_ladder AND trail_mult>0 so breakeven-only
            # configs (trail 0) keep exact legacy behaviour.
            try:
                if bool(getattr(s, "trailing_ladder", False)):
                    ladder = smart_exit.ladder_sl(
                        entry_price=pos.entry_price, direction=pos.direction,
                        r_distance=r_distance, r_multiple=r_multiple)
                    if ladder is not None:
                        if sign == 1:
                            new_sl = max(new_sl, ladder)  # type: ignore[arg-type]
                        else:
                            new_sl = min(new_sl, ladder)  # type: ignore[arg-type]
            except Exception:
                pass
        else:
            new_sl = be_price
        # only move when it actually improves the stop
        improves = (new_sl > (pos.stop_loss or 0)) if sign == 1 \
            else (new_sl < (pos.stop_loss or 1e18))
        if improves and abs(new_sl - pos.stop_loss) > 1e-9:
            try:
                old_sl = pos.stop_loss
                result = await broker.modify_stop_loss(pos.ticket, round(new_sl, 5))
                if result.ok:
                    pos.stop_loss = round(new_sl, 5)
                    out["moved_sl"] = True
                    out["new_sl"] = pos.stop_loss
                    out["old_sl"] = old_sl
                    move_kind = ("breakeven"
                                 if abs(pos.stop_loss - pos.entry_price) < 1e-9
                                 else "trailing")
                    # Persist the move back to the journal row — otherwise the
                    # monitor kept showing the ORIGINAL SL forever (migration
                    # 021 powers the "SL ถูกขยับ" badge on the monitor page).
                    execution.persist_sl_move(
                        db, str(pos.ticket or ""), pos.stop_loss, move_kind)
                    signal_log.log_event(
                        db=db, event="sl_moved", asset=str(pos.asset or ""),
                        direction=str(pos.direction or ""),
                        entry=pos.entry_price, stop_loss=pos.stop_loss,
                        ticket=str(pos.ticket or ""), source="auto",
                        reason=f"SL ย้ายไป {pos.stop_loss:g} ({move_kind})"
                               f" จาก {old_sl:g}")
                    # SL move LINE alert — stop_loss is CRITICAL so it pushes
                    # immediately (honours the notify_stop_loss switch inside
                    # NotificationService.notify). Fail-soft: never break guard.
                    try:
                        await notifier.notify(
                            pos.user_id, "stop_loss",
                            f"🔔 SL ขยับ ({move_kind})\n"
                            f"Asset: {pos.asset}\nDirection: {pos.direction}\n"
                            f"SL {old_sl:g} → {pos.stop_loss:g} @ {price:g} "
                            f"({r_multiple:.2f}R)\nTicket {pos.ticket}",
                        )
                    except Exception as exc:
                        log.debug("sl-move notify failed: %s", exc)
            except Exception as exc:
                log.warning("modify SL %s failed: %s", pos.ticket, exc)
    return out


async def guard_once(db, broker, notifier: NotificationService,
                     settings=None) -> dict:
    """One guard cycle. Returns a small summary for logs/tests.

    Besides the aggregate counters the summary carries three symbol lists
    (``sl_assets`` / ``closed_assets`` / ``skip_assets``) written into
    ``scheduler_runs.detail``: counters alone left the Logs > Guard tab
    unreadable ("moved_sl=2" with no idea WHICH pair moved), which is
    exactly the complaint that started this change.

    ``sl_assets`` items are ``ASSET@OLD_SL>NEW_SL`` so the tab answers
    "ขยับจากเท่าไร" (from what price) and not just the destination.
    """
    closed = 0
    moved = 0
    partials = 0
    smart_closed = 0
    smart_partials = 0
    emergency_closed = 0
    smart_skipped = 0
    # symbol-level audit for this round (capped when rendered)
    sl_assets: list[str] = []
    closed_assets: list[str] = []
    skip_assets: list[str] = []
    try:
        positions = await broker.all_positions()
    except Exception as exc:
        log.error("position guard cannot list positions: %s", exc)
        return {"checked": 0, "closed": 0, "moved_sl": 0, "partial_closed": 0,
                "smart_closed": 0, "smart_partials": 0,
                "smart_skipped": 0, "emergency_closed": 0,
                "sl_assets": "", "closed_assets": "", "skip_assets": ""}

    # Settings once per cycle (breakeven/trailing/partial knobs). Falls back
    # to schema defaults when the DB is unavailable.
    s = settings
    if s is None:
        try:
            s = execution.get_app_settings(db)
        except Exception:
            from app.models.schemas import AppSettings
            s = AppSettings()

    assets = sorted({str(p.asset or "").upper() for p in positions})

    # ---- Smart Exit shared context (once per cycle, fail-safe) ------------
    # EXIT PRIORITY 1-8: emergency → SL/TP → trailing(ladder) → AI score →
    # reversal → time → news. SL/TP/trailing/time live in this loop; the AI
    # engine supplies score/reversal/news/left-behind/volatility/profit.
    smart_on = bool(getattr(s, "smart_exit_enabled", True))
    snaps: dict[str, dict] = {}
    news_status, news_event = "SAFE", ""
    avg_hold = 4.0
    drawdown_pct = 0.0
    kill_engaged = False
    kill_triggers: list[str] = []
    # Priority 1 — Emergency Exit runs even when Smart Exit is OFF: the kill
    # switch is a safety path, not an AI feature. Single shared path
    # (execution.evaluate_kill) — same math as the entry gate and the
    # monitor banner, can never drift apart.
    if positions:
        try:
            ks = execution.evaluate_kill(db, s)
            kill_engaged = bool(getattr(ks, "engaged", False))
            kill_triggers = list(getattr(ks, "triggers", []) or [])
        except Exception as exc:
            log.debug("emergency kill check failed: %s", exc)
            kill_engaged = False

    # ---- Feed phase: live marks + snapshots + news IN PARALLEL -----------
    # These are independent fetches — running them one after another cost
    # their SUM (~55s) and overran the 1-min interval (see _GUARD_*_BUDGET).
    # Each branch keeps its own cap, so a slow snapshot/news feed degrades
    # Smart Exit to a blind HOLD but can never delay — let alone drop — the
    # live marks that drive the SL/TP safety path.
    live: dict[str, float] = {}
    if positions:
        marks_c, snaps_c, news_c = await asyncio.gather(
            _bounded(_live_marks(assets), _GUARD_MARKS_BUDGET),
            (_bounded(quotes.fetch_all_snapshots(assets), _GUARD_SNAP_BUDGET)
             if smart_on else _none()),
            (_bounded(_smart_exit_news(db, s), _GUARD_NEWS_BUDGET)
             if smart_on else _none()),
        )
        live = marks_c or {}
        snaps = snaps_c or {}
        if news_c:
            news_status, news_event = news_c
    if smart_on and positions:
        try:
            avg_hold = _avg_hold_days(db)
        except Exception:
            avg_hold = 4.0
        try:
            drawdown_pct = execution.equity_drawdown_pct(
                db, float(getattr(s, "capital", 0) or 0))
        except Exception:
            drawdown_pct = 0.0
    # Emergency closes aggregate into ONE LINE message after the loop
    # (per-close journal + signal_log stay per-ticket for the audit trail).
    emergency_lines: list[str] = []
    emergency_pnl = 0.0
    emergency_user = ""

    for pos in positions:
        # refresh mark price: live feed first, then broker-native, then book
        price = live.get(pos.asset.upper()) or 0.0
        if not price:
            try:
                price = await broker.mark_price(pos.ticket)
            except Exception:
                price = 0.0
        if not price:
            try:
                price = await broker.quote(pos.asset)
            except Exception:
                price = 0.0
        if not price:
            continue
        pos.current_price = price

        # ---- Priority 1: Emergency Exit (kill switch engaged) ------------
        # Closes EVERYTHING immediately — skips management/SL/TP/smart/time.
        # Notify is aggregated AFTER the loop (one LINE message per cycle).
        if kill_engaged:
            try:
                result = await broker.close_position(pos.ticket)
            except Exception as exc:
                log.warning("emergency close %s failed: %s", pos.ticket, exc)
                continue
            if not getattr(result, "ok", False):
                log.warning("emergency close %s rejected: %s", pos.ticket,
                            getattr(result, "message", ""))
                continue
            pnl = execution.PaperBrokerPnl.compute(
                pos, s, asset=str(pos.asset or ""))
            execution.close_trade_rows(db, pos.ticket, price, pnl, "emergency")
            signal_log.log_event(
                db=db, event="closed", asset=str(pos.asset or ""),
                direction=str(pos.direction or ""),
                entry=pos.entry_price, exit_price=price, pnl=pnl,
                ticket=str(pos.ticket or ""), source="auto",
                reason="🚨 Emergency Exit (kill switch: "
                       + ("; ".join(kill_triggers)[:200] or "engaged")
                       + f") — ปิดที่ {price:g}")
            emergency_closed += 1
            closed += 1
            closed_assets.append(f"{pos.asset}:kill")
            emergency_pnl += float(pnl or 0)
            emergency_user = emergency_user or str(getattr(pos, "user_id", "") or "")
            emergency_lines.append(
                f"• {pos.asset} {pos.direction} "
                f"{pos.entry_price:g}→{price:g} PnL {pnl:+,.2f}")
            continue

        # ---- management pass: breakeven / trailing / partial (TP1) ----
        try:
            mgmt = await _manage_position(db, broker, pos, price, s, notifier)
            if mgmt.get("moved_sl"):
                moved += 1
                _new = mgmt.get("new_sl")
                _old = mgmt.get("old_sl")
                # "EURCHF@0.93624>0.94337" — from > to, so the Logs > Guard
                # chip answers "ขยับจากเท่าไร" without opening the journal.
                if _new is not None and _old is not None:
                    sl_assets.append(f"{pos.asset}@{_old:g}>{_new:g}")
                elif _new is not None:
                    sl_assets.append(f"{pos.asset}@{_new:g}")
                else:
                    sl_assets.append(str(pos.asset or ""))
            if mgmt.get("partial_closed"):
                partials += 1
                closed_assets.append(f"{pos.asset}:tp1")
        except Exception as exc:
            log.warning("manage %s failed: %s", pos.ticket, exc)

        # ---- Priorities 2-3: SL/TP hard stops win over everything below --
        sl, tp = pos.stop_loss, pos.take_profit
        hit_sl = sl is not None and (
            (pos.direction == "BUY" and price <= sl)
            or (pos.direction == "SELL" and price >= sl))
        hit_tp = tp is not None and (
            (pos.direction == "BUY" and price >= tp)
            or (pos.direction == "SELL" and price <= tp))
        if hit_sl or hit_tp:
            pass  # handled by the SL/TP close block after smart-exit skip
        elif smart_on:
            # ---- Priorities 5, 6, 8: AI score / reversal / news ---------
            # Only when no hard stop was hit. Blind HOLD when the indicator
            # feed is down (no snapshot) — never close without indicators.
            # Fail-safe: any eval error just skips to SL/TP + time stop.
            # Every skip lands in smart_skipped (summary + scheduler log) so
            # a badge that says CLOSE with an untouched position is
            # explainable: the guard never evaluated, or the broker rejected.
            try:
                snap = snaps.get(str(pos.asset or "").upper(), {}) or {}
                if snap:
                    age = _position_age_days(pos, db)
                    decision = smart_exit.evaluate_exit(
                        asset=str(pos.asset or ""),
                        direction=str(pos.direction or ""),
                        entry_price=float(pos.entry_price or 0),
                        price=price, stop_loss=pos.stop_loss,
                        age_days=age, settings=s, snapshot=snap,
                        news_status=news_status, news_event=news_event,
                        avg_hold_days=avg_hold, drawdown_pct=drawdown_pct)
                    rec = str(getattr(decision, "recommendation", "HOLD")
                              or "HOLD")
                    if rec in ("PARTIAL_25", "PARTIAL_50", "CLOSE"):
                        applied = await _apply_smart_exit(
                            db, broker, pos, price, decision, s, notifier)
                        if applied.get("closed"):
                            closed += 1
                            smart_closed += 1
                            closed_assets.append(f"{pos.asset}:smart")
                            continue  # fully closed — skip SL/TP + time stop
                        if applied.get("partial_closed"):
                            partials += 1
                            smart_partials += 1
                            closed_assets.append(
                                f"{pos.asset}:"
                                + ("smart25" if rec == "PARTIAL_25"
                                   else "smart50"))
                            # remainder falls through to SL/TP + time-stop
                        else:
                            # Engine fired but nothing executed (broker reject
                            # or TP1 already done) — NOT a silent no-op.
                            smart_skipped += 1
                            skip_assets.append(f"{pos.asset}:not_applied")
                            log.warning(
                                "smart-exit %s fired %s (%s) but not applied",
                                pos.ticket, rec,
                                getattr(decision, "trigger", ""))
                else:
                    smart_skipped += 1
                    skip_assets.append(f"{pos.asset}:no_snapshot")
                    log.debug("smart-exit skip %s: no snapshot (blind HOLD)",
                              pos.ticket)
            except Exception as exc:
                smart_skipped += 1
                skip_assets.append(f"{pos.asset}:eval_error")
                log.warning("smart-exit eval %s failed: %s", pos.ticket, exc)

        if not (hit_sl or hit_tp):
            # ---- time stop: close stale positions (max_hold_days) ----
            # SL/TP always wins (checked first); this only fires when neither
            # was touched but the trade has simply been open too long.
            # Age uses the SAME journal-first _position_age_days as Smart
            # Exit — opened_at alone would drift from the badge after deploys.
            max_hold = int(getattr(s, "max_hold_days", 0) or 0)
            if max_hold > 0:
                age_days = _position_age_days(pos, db)
                # R-exemption (2026-09-11, option "ก"): age alone is not a
                # reason to cut a live winner. The left_behind rule above is
                # R-conditional and spares profitable positions; without this
                # check the R-BLIND time stop re-closed exactly what
                # left_behind let through — a +3R position could be killed on
                # day 5. R uses the ORIGINAL stop so a trailed/breakeven SL
                # can't inflate it (see _r_multiple_at).
                ts_min_r = float(getattr(s, "time_stop_min_r", 1.0) or 0)
                r_now = _r_multiple_at(pos, price, db) if ts_min_r > 0 else 0.0
                if age_days >= max_hold and (ts_min_r <= 0 or r_now < ts_min_r):
                    age_txt = f"{age_days:.1f}"
                    result = await broker.close_position(pos.ticket)
                    if not result.ok:
                        log.warning("time-stop close %s failed: %s",
                                    pos.ticket, result.message)
                        continue
                    pnl = execution.PaperBrokerPnl.compute(
                        pos, s, asset=str(pos.asset or ""))
                    execution.close_trade_rows(db, pos.ticket, price, pnl, "time")
                    signal_log.log_event(
                        db=db, event="closed", asset=str(pos.asset or ""),
                        direction=str(pos.direction or ""),
                        entry=pos.entry_price, exit_price=price, pnl=pnl,
                        ticket=str(pos.ticket or ""), source="auto",
                        reason=f"หมดเวลาถือไม้ (time stop) ถือ {age_txt} วัน "
                               f"เกิน {max_hold} วัน และกำไร {r_now:+.2f}R "
                               f"< {ts_min_r:g}R — ปิดที่ {price:g}")
                    closed += 1
                    closed_assets.append(f"{pos.asset}:time")
                    try:
                        await notifier.notify(
                            pos.user_id, "trade_closed",
                            f"⏱ Position Closed (TIME STOP)\n"
                            f"Asset: {pos.asset}\nDirection: {pos.direction}\n"
                            f"Entry: {pos.entry_price:g} → Exit: {price:g}\n"
                            f"Held: {age_days:.1f} days (limit {max_hold})\n"
                            f"R: {r_now:+.2f} (need ≥ {ts_min_r:g} to survive)\n"
                            f"PnL: {pnl:+,.2f}",
                        )
                    except Exception as exc:
                        log.error("time-stop notify failed: %s", exc)
                elif age_days >= max_hold:
                    log.info("time stop spared %s: %+.2fR ≥ %.2gR after %.1f days",
                             pos.ticket, r_now, ts_min_r, age_days)
            continue

        reason = "sl" if hit_sl else "tp"
        result = await broker.close_position(pos.ticket)
        if not result.ok:
            log.warning("close %s failed: %s", pos.ticket, result.message)
            continue

        pnl = execution.PaperBrokerPnl.compute(pos, s, asset=str(pos.asset or ""))
        execution.close_trade_rows(db, pos.ticket, price, pnl, reason)
        # Honest label: `reason` stays "sl"/"tp" for the journal + badge, but a
        # stop-out whose stop had been moved ABOVE entry is a win — calling it
        # "ตัดขาดทุน (SL)" made the log contradict the PnL on the same row.
        if not hit_sl:
            label = "ปิดกำไร (TP) ที่ "
        else:
            moved_kind = _sl_move_kind(pos, db)
            if pnl > 1e-9:
                label = ("ปิดทำกำไรที่จุดกันทุน (trailing SL) ที่ "
                         if moved_kind == "trailing"
                         else "ปิดที่จุดกันทุน (breakeven) ที่ ")
            elif moved_kind:
                label = ("ปิดเสมอตัวที่จุดกันทุน (breakeven) ที่ "
                         if moved_kind == "breakeven"
                         else "ปิดที่จุดกันทุน (trailing SL) ที่ ")
            else:
                label = "ตัดขาดทุน (SL) ที่ "
        signal_log.log_event(
            db=db, event="closed", asset=str(pos.asset or ""),
            direction=str(pos.direction or ""), entry=pos.entry_price,
            exit_price=price, pnl=pnl, ticket=str(pos.ticket or ""),
            source="auto",
            reason=label + f"{price:g}")
        closed += 1
        closed_assets.append(
            f"{pos.asset}:{'sl' if hit_sl else 'tp'}@{price:g}")
        try:
            emoji = "🛑" if hit_sl else "🎯"
            await notifier.notify(
                pos.user_id, "stop_loss" if hit_sl else "trade_closed",
                f"{emoji} Position Closed ({'STOP LOSS' if hit_sl else 'TAKE PROFIT'})\n"
                f"Asset: {pos.asset}\nDirection: {pos.direction}\n"
                f"Entry: {pos.entry_price:g} → Exit: {price:g}\n"
                f"PnL: {pnl:+,.2f}",
            )
        except Exception as exc:
            log.error("close notify failed: %s", exc)

    # Single aggregated emergency message (journal + signal_log already
    # recorded per-ticket above — the audit trail stays granular).
    if emergency_closed:
        try:
            body = "\n".join(emergency_lines[:10])
            if len(emergency_lines) > 10:
                body += f"\n…และอีก {len(emergency_lines) - 10} ไม้"
            await notifier.notify(
                emergency_user, "trade_closed",
                f"🚨 Emergency Exit (KILL SWITCH) — ปิด {emergency_closed} ไม้\n"
                + body + "\n"
                f"รวม PnL {emergency_pnl:+,.2f}\n"
                + ("; ".join(kill_triggers)[:200] or "kill switch engaged"),
            )
        except Exception as exc:
            log.error("emergency notify failed: %s", exc)

    return {"checked": len(positions), "closed": closed,
            "moved_sl": moved, "partial_closed": partials,
            "smart_closed": smart_closed, "smart_partials": smart_partials,
            "smart_skipped": smart_skipped,
            "emergency_closed": emergency_closed,
            # symbol-level audit — see docstring. Format of one item:
            #   sl_assets     "EURCHF@0.93624>0.94337"  (asset@old SL>new SL;
            #                 "@0.94337" alone = old SL unknown / legacy row)
            #   closed_assets "EURCHF:sl@1.2345"        (asset:reason[@exit])
            #   skip_assets   "GBPCHF:no_snapshot"
            # ";" separates items (values never contain a comma, so the
            # "k=v, k=v" line stays unambiguous for readers and regexes).
            # Cap 4 per list: the worst case (4 long tokens each, all three
            # lists full, 2-digit counters) lands ~440 chars, inside the
            # 480-char budget of _summarize / the 500-char DB column. Cap 6
            # would overflow and cut a price in half mid-token.
            "sl_assets": ";".join(sl_assets[:4]),
            "closed_assets": ";".join(closed_assets[:4]),
            "skip_assets": ";".join(skip_assets[:4])}


def run_guard_blocking(db, broker, notifier) -> dict:
    """Sync wrapper for asyncio.to_thread callers (matches portfolio_monitor style)."""
    return asyncio.run(guard_once(db, broker, notifier))


async def rehydrate_book(db, broker) -> int:
    """Rebuild the in-memory broker book from open paper_trades rows.

    The PaperBroker book is in-memory, so every redeploy/restart on Render
    silently wiped it — open positions stayed "open" in the DB (shown on the
    monitor) while guard_once saw an empty book and never enforced SL/TP
    again. On startup, re-open any DB row still marked open so the guard
    can close it when the live feed touches its SL/TP.

    Returns the number of positions restored.
    """
    try:
        rows = db.select("paper_trades", filters={"status": "open"}, limit=200)
    except Exception as exc:
        log.warning("rehydrate: cannot read open paper_trades: %s", exc)
        return 0
    book = getattr(broker, "_positions", None)
    if book is None:
        log.warning("rehydrate: broker exposes no in-memory book; skipped")
        return 0
    seq = int(getattr(broker, "_seq", 0) or 0)
    restored = 0
    # Tickets already live in the broker book BEFORE rehydrate (e.g. an order
    # placed earlier in this process) are authoritative — matching DB rows are
    # skipped. Duplicates WITHIN the DB rows themselves are re-ticketed.
    preexisting = set(book)
    seen = set(preexisting)
    for row in rows or []:
        ticket = str(row.get("ticket") or "")
        if not ticket or ticket in preexisting:
            continue
        if ticket in seen:
            # Duplicate ticket: the broker's order sequence restarts at 1 on
            # every deploy, so a NEW trade can re-issue a ticket identical to
            # a pre-restart row still "open" in the DB (observed: GBPUSD
            # PAPER-000001 from 11:18 vs a new AUDUSD PAPER-000001). Re-issue
            # the rehydrated row a fresh ticket and rewrite the DB row so
            # marks/closes map 1:1 again.
            seq += 1
            new_ticket = f"PAPER-{seq:06d}"
            try:
                db.update("paper_trades", str(row.get("id") or ""),
                          {"ticket": new_ticket})
            except Exception as exc:
                log.warning("rehydrate: cannot re-ticket %s: %s", ticket, exc)
                continue
            log.warning("rehydrate: duplicate ticket %s — row %s re-issued as %s",
                        ticket, row.get("id"), new_ticket)
            ticket = new_ticket
        try:
            book[ticket] = Position(
                ticket=ticket,
                user_id=str(row.get("user_id") or ""),
                asset=str(row.get("asset") or "").upper(),
                direction=str(row.get("direction") or "BUY").upper(),
                volume=float(row.get("volume") or 0),
                entry_price=float(row.get("entry_price") or 0),
                stop_loss=float(row["stop_loss"]) if row.get("stop_loss") is not None else None,
                take_profit=float(row["take_profit"]) if row.get("take_profit") is not None else None,
                current_price=float(row.get("entry_price") or 0),
            )
            # journal row id — lets the guard persist partial_done back to DB
            book[ticket].row_id = str(row.get("id") or "")  # type: ignore[attr-defined]
            # TP1 already fired for this position before the restart — carry
            # the flag into the book or the guard would partial-close again.
            book[ticket].partial_done = bool(row.get("partial_done"))  # type: ignore[attr-defined]
            # Time-stop age must survive restarts — restore opened_at from the
            # journal row's created_at (Position's default is "now", which
            # would silently reset the clock on every deploy).
            opened_at = None
            created = row.get("created_at")
            if created:
                try:
                    opened_at = datetime.fromisoformat(
                        str(created).replace("Z", "+00:00"))
                    if opened_at.tzinfo is None:
                        opened_at = opened_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    opened_at = None
            if opened_at is not None:
                book[ticket].opened_at = opened_at
            seen.add(ticket)
            restored += 1
            # Walk the order sequence past every restored ticket so future
            # place_order() calls can never collide with restored ones.
            if ticket.startswith("PAPER-"):
                try:
                    seq = max(seq, int(ticket.split("-", 1)[1]))
                except (IndexError, ValueError):
                    pass
        except Exception as exc:
            log.warning("rehydrate: skip %s: %s", ticket or "?", exc)
    if hasattr(broker, "_seq"):
        broker._seq = max(getattr(broker, "_seq", 0), seq)
    if restored:
        log.info("rehydrate: restored %d open position(s) into the broker book "
                 "(order sequence at %d)", restored, seq)
    return restored
