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

Priority 0 is the EMERGENCY EXIT: when the kill switch is engaged every open
position is closed at once. It is DEFERRED while an unanswered limit-expansion
request is on the table (the prompt says "ลิมิตยังไม่ถูกแตะต้อง") — until the owner
answers, and for the WHOLE confirmation window (``kill_expand_ttl_min``, default
180 min) if they stay silent. When the window runs out the TIMEOUT POLICY decides,
not the guard: the +5% expansion is applied automatically (and pushed), the
account is re-judged against the widened limits, and the guard closes only if it
is STILL over them — or if the expansion could not be written at all.

Real broker adapters (MT5/OANDA) enforce SL/TP server-side; their close events
still flow through close_trade_rows so the journal stays authoritative.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from app.engine import smart_exit
from app.integrations import quotes
from app.integrations.brokers import Position
from app.models.schemas import G, S
from app.services import execution
from app.services import limit_expand
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
# Canonical values: AppSettings.guard_marks/snap/news_timeout_s (Settings page).
_GUARD_MARKS_BUDGET = S("guard_marks_timeout_s")   # live spot marks (safety path)
_GUARD_SNAP_BUDGET = S("guard_snap_timeout_s")    # Smart-Exit snapshots (AI score enhancement)
_GUARD_NEWS_BUDGET = S("guard_news_timeout_s")    # Smart-Exit news calendar


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

    DAILY fallback rates are refused here. The spot chain falls through to
    exchangerate.com when the intraday feed (Yahoo) is down, and that source
    publishes ONE rate per business day — it can sit on the wrong side of a
    stop by a whole percent. Prod 2026-09-14: Yahoo timed out for NZDUSD, the
    daily rate 0.5812 (already 2h40m old, real market 0.5766) cleared TP
    0.58081, so PAPER-000005 was booked \"closed TP 0.5812\" +14.70 and its
    trailing SL moved to 0.58057 — all from a price that never traded. A
    position with no intraday mark is left untouched for the cycle: a missed
    trail is a normal outage, a fabricated fill is a corrupted journal.
    """
    try:
        prices, _failures = await quotes.fetch_spot_prices(assets)
        out: dict[str, float] = {}
        for asset, price in (prices or {}).items():
            try:
                p = float(price or 0)
            except (TypeError, ValueError):
                continue
            if p <= 0:
                continue
            if quotes.spot_source(asset) == "daily":
                log.warning("guard: %s mark %.6g is a DAILY fallback rate — "
                            "not used for SL/TP this cycle", asset, p)
                continue
            out[asset] = p
        return out
    except Exception as exc:
        log.warning("live marks unavailable (%s) — falling back to broker book", exc)
        return {}


async def _conversion_rates(positions: list[Position],
                            marks: dict[str, float]) -> dict[str, float]:
    """Spot map used to convert each position's raw PnL into USD.

    Delegates to ``execution.fetch_pnl_rates`` which seeds from the marks
    already in hand and fetches only the missing ``<quote>USD`` legs. Missing
    rates are simply absent — `PaperBrokerPnl.compute` then returns None and
    the journal shows no PnL instead of a guessed dollar figure (fail-closed).
    """
    assets = [str(getattr(pos, "asset", "") or "") for pos in positions]
    return await execution.fetch_pnl_rates(assets, seed=marks)


def _pnl_text(pnl) -> str:
    """Format a PnL figure; None (no conversion rate) reads 'n/a'.

    ``sign × price_diff × lots × contract`` is in the QUOTE currency and must
    be converted to USD. When no trusted rate exists the journal stores None
    and the LINE line says n/a — never a guessed dollar amount.
    """
    if pnl is None:
        return "n/a"
    try:
        return f"{float(pnl):+,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _resolve_initial_volume(db, pos, row: dict | None = None) -> float | None:
    """Original size of a position, for the "closed / original" display.

    `paper_trades.volume` is the REMAINING size once a partial close fires
    (migration 047), so the monitor needs the ORIGINAL size to show
    "0.01/0.02". Resolution order (first hit wins):
      1. the row's `initial_volume` (migration 048, written at insert),
      2. the `order_opened` signal-log volume for this ticket (legacy rows),
      3. the row's current `volume` (never partial-closed → it IS the original).
    Returns None when nothing is known — the UI then shows the bare volume.
    Never raises.
    """
    try:
        if row is None:
            rows = db.select("paper_trades",
                             filters={"ticket": str(getattr(pos, "ticket", "") or "")},
                             limit=1)
            row = rows[0] if rows else {}
        iv = (row or {}).get("initial_volume")
        if iv is not None:
            return float(iv)
        ticket = str(getattr(pos, "ticket", "") or "")
        if ticket:
            logs = db.select("signal_logs",
                             filters={"ticket": ticket, "event": "order_opened"},
                             order="created_at", limit=1)
            if logs and logs[0].get("volume") is not None:
                return float(logs[0]["volume"])
        vol = (row or {}).get("volume")
        return float(vol) if vol is not None else None
    except Exception as exc:
        log.debug("initial_volume resolve failed: %s", exc)
        return None


def _atr_for(pos: Position, fallback_distance: float, db=None,
             proxy_mult: Optional[float] = None) -> float:
    """ATR estimate for trailing: proxy_mult × the ORIGINAL SL distance.

    The guard has no candle history per ticket; the SL distance the signal
    was sized from is a stable proxy (SL = 1.5 × ATR at entry by default).
    None → canonical AppSettings.guard_atr_proxy_mult (Settings page).

    CRITICAL: the denominator must be the INITIAL stop, never the current
    one. Breakeven / trailing move the stop toward (or past) entry, so
    `abs(entry - stop_loss)` collapses as the trail tightens — the ATR proxy
    shrinks with it and the trail hugs price ever tighter (ratchet collapse).
    That strangled every winner at ~+0.7R while losers ran the full −1.0R,
    inverting reward:risk. `initial_stop_loss` (migration 021, the same field
    `_r_multiple_at` and the monitor badge read) is used first; the current
    SL is only a fallback when the journal row is unavailable.
    """
    risk = None
    if db is not None:
        try:
            rows = db.select(
                "paper_trades",
                filters={"ticket": str(getattr(pos, "ticket", "") or "")},
                limit=1)
            if rows:
                risk = rows[0].get("initial_stop_loss")
        except Exception:
            risk = None
    if risk is None:
        risk = getattr(pos, "stop_loss", None)
    if proxy_mult is None:
        from app.models.schemas import S as _S
        proxy_mult = float(_S("guard_atr_proxy_mult"))
    if risk is None:
        return fallback_distance * proxy_mult
    return abs(pos.entry_price - float(risk)) * proxy_mult


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


def _utcnow():
    """Current UTC time — a seam so tests can pin the clock.

    Age rules are weekend-aware (see _position_age_days), so a test that
    wants to prove "a position held over the weekend is only 2h old" must
    control now(). Patching datetime.now globally is fragile; this one
    indirection is not.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _position_age_days(pos: Position, db=None) -> float:
    """Age of a position in TRADING days — journal created_at first, opened_at
    fallback.

    The journal row is authoritative (same created_at the monitor's
    exit_info_for reads), so the guard and the monitor can never disagree
    on age. opened_at (restored from created_at by rehydrate_book) is only
    a fallback for rows missing from the DB. Never raises; 0.0 when
    nothing is known (fresh position).

    WHY trading days (2026-09-19): every age rule here (time stop,
    NO-POSITION-LEFT-BEHIND) asks "has this trade had enough time to work?" —
    a question about time the MARKET was open. While the market is shut the
    price is frozen, so the position is parked, not aging. Counting the
    weekend added ~2 days per weekend, so a position at 4.9 days on Friday
    20:59 UTC was cut by the time stop in the first cycle after the Sunday
    21:00 UTC reopen. Weekend closure is now subtracted via the shared
    schemas.market_open_days_between.
    """
    from datetime import datetime, timezone
    from app.models import schemas as _schemas
    if db is not None:
        try:
            rows = db.select("paper_trades",
                             filters={"ticket": str(getattr(pos, "ticket", "") or "")},
                             limit=1)
            if rows:
                created = rows[0].get("created_at")
                dt = execution._parse_dt(created)
                if dt is not None:
                    return max(0.0, _schemas.market_open_days_between(
                        dt, _utcnow()))
        except Exception:
            pass
    opened = getattr(pos, "opened_at", None)
    if opened is not None:
        try:
            return max(0.0, _schemas.market_open_days_between(
                opened, _utcnow()))
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
        return S("avg_hold_fallback_days")


async def _apply_smart_exit(db, broker, pos: Position, price: float,
                            decision, s,
                            notifier: NotificationService,
                            rates: dict[str, float] | None = None) -> dict:
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
        # A slice that would consume the whole remaining size is a full exit,
        # not a scale-out — close it properly so the journal keeps a reason
        # (same rule as the TP1 path).
        if slice_vol >= float(pos.volume or 0):
            await _close_whole_position(
                db, broker, pos, price, s, notifier, rates=rates,
                reason=reason_prefix,
                reason_text=f"{reason_prefix} แบ่งปิดครบขนาด — ปิดทั้งไม้")
            out["closed"] = True
            return out
        if slice_vol <= 0:
            return out
        # Same double-subtract guard as the TP1 path: snapshot BEFORE the
        # broker call (PaperBroker mutates pos.volume in place).
        pre_vol = float(pos.volume or 0)
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
        after_vol = float(getattr(pos, "volume", pre_vol) or 0)
        if after_vol < pre_vol - 1e-9:
            remaining_vol = round(max(0.0, after_vol), 2)
        else:
            remaining_vol = round(max(0.0, pre_vol - slice_vol), 2)
        try:
            pos.volume = remaining_vol
        except Exception:
            pass
        try:
            row_id = str(getattr(pos, "row_id", "") or "")
            if not row_id:
                rows = db.select("paper_trades", filters={"ticket": ticket}, limit=1)
                row_id = str(rows[0].get("id") or "") if rows else ""
            if row_id:
                patch = {"partial_done": True, "volume": remaining_vol}
                init_vol = _resolve_initial_volume(db, pos)
                if init_vol is not None:
                    patch["initial_volume"] = init_vol
                db.update("paper_trades", row_id, patch)
        except Exception as exc:
            log.debug("smart-exit partial_done persist failed: %s", exc)
        # Realized PnL of the CLOSED SLICE only (USD) — see the TP1 path.
        slice_pnl = execution.PaperBrokerPnl.compute(
            SimpleNamespace(
                direction=pos.direction, asset=pos.asset,
                entry_price=pos.entry_price,
                current_price=price, volume=slice_vol),
            s, asset=asset, rates=rates)
        signal_log.log_event(
            db=db, event="closed", asset=asset, direction=direction,
            entry=pos.entry_price, exit_price=price, ticket=ticket,
            volume=slice_vol, pnl=slice_pnl, source="auto",
            reason=f"{reason_prefix} แบ่งปิด {slice_vol:g} lots @ {price:g} — {why}")
        try:
            await notifier.notify(
                pos.user_id, "trade_closed",
                f"🧠 Smart Exit ({rec})\nAsset: {asset}\nDirection: {direction}\n"
                f"Closed: {slice_vol:g} lots @ {price:g}\n"
                f"PnL: {_pnl_text(slice_pnl)}\n"
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
    pnl = execution.PaperBrokerPnl.compute(pos, s, asset=asset, rates=rates)
    execution.close_trade_rows(db, ticket, price, pnl, reason_prefix,
                               asset=asset, direction=direction)
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
            f"({getattr(decision, 'quality', '?')})\nPnL: {_pnl_text(pnl)}\n{why}")
    except Exception as exc:
        log.error("smart-exit close notify failed: %s", exc)
    return out


async def _close_whole_position(db, broker, pos: Position, price: float, s,
                                notifier: NotificationService,
                                rates: dict[str, float] | None = None,
                                reason: str = "manual",
                                reason_text: str = "") -> bool:
    """Close the ENTIRE position and journal it with a real close_reason.

    Used when a partial-close slice would consume the whole remaining size
    (partial_pct=100, or a 1-lot position whose 50% rounds up to the full
    size). Without this the row was left at volume 0 / status open with
    ``close_reason=None`` — the monitor's "เหตุผลปิด" column then showed
    nothing for a trade that really did close (prod 2026-09-23 AUDNZD).

    Returns True when the broker accepted the close. Never raises.
    """
    ticket = str(getattr(pos, "ticket", "") or "")
    asset = str(getattr(pos, "asset", "") or "")
    direction = str(getattr(pos, "direction", "") or "")
    try:
        result = await broker.close_position(ticket)
    except Exception as exc:
        log.warning("close-whole %s failed: %s", ticket, exc)
        return False
    if not getattr(result, "ok", False):
        log.warning("close-whole %s rejected: %s", ticket,
                    getattr(result, "message", ""))
        return False
    pnl = execution.PaperBrokerPnl.compute(pos, s, asset=asset, rates=rates)
    execution.close_trade_rows(db, ticket, price, pnl, reason,
                               asset=asset, direction=direction)
    signal_log.log_event(
        db=db, event="closed", asset=asset, direction=direction,
        entry=pos.entry_price, exit_price=price, pnl=pnl, ticket=ticket,
        source="auto",
        reason=f"{reason_text or reason} @ {price:g}")
    try:
        await notifier.notify(
            pos.user_id, "trade_closed",
            f"✅ ปิดทั้งไม้\nAsset: {asset}\nDirection: {direction}\n"
            f"Entry: {pos.entry_price:g} → Exit: {price:g}\n"
            f"PnL: {_pnl_text(pnl)}\n{reason_text or reason}")
    except Exception as exc:
        log.debug("close-whole notify failed: %s", exc)
    return True


async def _manage_position(db, broker, pos: Position, price: float,
                           s, notifier: NotificationService,
                           discretionary_block: str = "",
                           rates: dict[str, float] | None = None) -> dict:
    """Breakeven / trailing / partial-close pass for ONE position.

    Returns {"moved_sl": bool, "partial_closed": bool, "new_sl": float,
    "old_sl": float, "partial_volume": float} for the summary. Never raises —
    a failed broker call just skips the action this cycle.

    ``old_sl`` is the stop BEFORE the move, so the audit line can say WHICH
    pair moved FROM where to where ("EURCHF@0.93624>0.94337") instead of
    only the destination.

    ``discretionary_block`` (non-empty = market closed) suppresses EVERY
    action in this pass — the TP1 partial close AND the breakeven/trailing SL
    move. Owner rule 2026-09-19 (final): the real broker cannot close or
    modify a stop while the market is shut, and the frozen mark makes the
    R-multiple meaningless. Everything is deferred to the first open cycle.
    """
    out = {"moved_sl": False, "partial_closed": False,
           "new_sl": None, "old_sl": None, "partial_volume": None}
    # A position whose whole size was closed by partials (volume 0) has
    # nothing left to manage. Before migration 047 the reduced volume was
    # never persisted, so a re-fired TP1 could drive the stored size to 0
    # while the row stayed `open` — the guard then kept moving a stop on a
    # zero-size position forever. Close it out and stop.
    if float(getattr(pos, "volume", 0) or 0) <= 0:
        try:
            row_id = str(getattr(pos, "row_id", "") or "")
            if not row_id:
                rows = db.select("paper_trades",
                                 filters={"ticket": str(pos.ticket or "")},
                                 limit=1)
                row_id = str(rows[0].get("id") or "") if rows else ""
            if row_id:
                # close_reason + closed_at + journal event: a swept row must
                # not look like an unexplained close (prod 2026-09-25
                # PAPER-000109 showed closed/pnl 5.2/reason None/closed_at
                # None — and realized_stats windows on closed_at, so its
                # +5.2 never appeared in "PnL วันนี้" either).
                from datetime import datetime as _dt, timezone as _tz
                db.update("paper_trades", row_id,
                          {"status": "closed",
                           "close_reason": "zero_volume_swept",
                           "closed_at": _dt.now(_tz.utc).isoformat()})
                log.info("position %s had zero volume — marked closed",
                         pos.ticket)
                signal_log.log_event(
                    db=db, event="closed",
                    asset=str(getattr(pos, "asset", "") or ""),
                    direction=str(getattr(pos, "direction", "") or ""),
                    entry=getattr(pos, "entry_price", None),
                    ticket=str(pos.ticket or ""), source="auto",
                    reason="ปิดไม้ที่ volume เหลือ 0 หลังแบ่งปิดครบขนาด "
                           "(zero-volume sweep)")
        except Exception as exc:
            log.debug("zero-volume close persist failed: %s", exc)
        return out
    if pos.stop_loss is None or pos.entry_price <= 0:
        return out

    sign = 1 if pos.direction == "BUY" else -1
    # R unit = the ORIGINAL risk distance, never the current stop. Breakeven /
    # trailing move the stop toward entry, so `abs(entry - stop_loss)` would
    # collapse and report a fantasy R (and shrink the trailing ATR proxy).
    # `initial_stop_loss` (migration 021) is the source of truth; the current
    # SL is only a fallback when the journal row is unavailable.
    r_distance = abs(pos.entry_price - pos.stop_loss)
    try:
        rows = db.select("paper_trades",
                         filters={"ticket": str(pos.ticket or "")}, limit=1)
        if rows and rows[0].get("initial_stop_loss") is not None:
            r_distance = abs(pos.entry_price - float(rows[0]["initial_stop_loss"]))
    except Exception:
        pass
    if r_distance <= 0:
        return out
    profit_distance = (price - pos.entry_price) * sign  # >0 when winning
    r_multiple = profit_distance / r_distance

    be_trigger = float(G(s, "breakeven_trigger_r", zero_as_missing=False) or 0)
    trail_mult = float(G(s, "trail_atr_mult", zero_as_missing=False) or 0)
    partial_pct = float(G(s, "partial_close_pct", zero_as_missing=False) or 0)
    partial_trigger = float(G(s, "partial_trigger_r", zero_as_missing=False) or 0)

    # ---- 1. partial close (TP1) — once per position -----------------------
    # Skipped while the market is closed: TP1 realises a PnL, so it is a
    # discretionary exit (owner 2026-09-19). It fires on the next open cycle.
    if partial_pct > 0 and partial_trigger > 0 and r_multiple >= partial_trigger \
            and not getattr(pos, "partial_done", False) \
            and not discretionary_block:
        slice_vol = round(pos.volume * partial_pct / 100.0, 2)
        # A slice that would close the WHOLE remaining size (partial_pct=100,
        # or a 1-lot position where 50% rounds up to the full size) is not a
        # scale-out — it is a full exit. Close the position properly so the
        # journal carries a close_reason + PnL instead of leaving a zero-volume
        # row that only the next cycle's zero-volume guard would sweep up
        # (prod 2026-09-23: PAPER-000080 closed in slices until volume hit 0
        # with close_reason=None, so the monitor showed no reason at all).
        if slice_vol >= pos.volume:
            await _close_whole_position(
                db, broker, pos, price, s, notifier, rates=rates,
                reason="tp", reason_text="ปิดบางส่วนครบขนาด (TP1) — ปิดทั้งไม้")
            return out
        if slice_vol > 0:
            # Snapshot BEFORE the broker call: PaperBroker mutates pos.volume
            # in place (0.02 → 0.01), a non-mutating adapter leaves it whole.
            # The remainder is what's LEFT, not left-minus-slice-again —
            # subtracting twice zeroed the DB row while 0.01 lots were still
            # live (prod 2026-09-25 PAPER-000109: monitor 0/0.02, PnL 0).
            pre_vol = float(pos.volume or 0)
            try:
                result = await broker.partial_close(pos.ticket, slice_vol)
                if result.ok:
                    out["partial_closed"] = True
                    out["partial_volume"] = slice_vol
                    pos.partial_done = True  # type: ignore[attr-defined]
                    # Remaining size after the scale-out.
                    after_vol = float(getattr(pos, "volume", pre_vol) or 0)
                    if after_vol < pre_vol - 1e-9:
                        remaining_vol = round(max(0.0, after_vol), 2)
                    else:
                        remaining_vol = round(max(0.0, pre_vol - slice_vol), 2)
                    try:
                        pos.volume = remaining_vol
                    except Exception:
                        pass
                    # Persist the flag AND the reduced volume — otherwise a
                    # restart re-fires TP1 (partial_done lost) and the monitor
                    # keeps showing the original size (volume never updated).
                    # `initial_volume` (migration 048) is backfilled here for
                    # legacy rows so the UI can show "closed / original".
                    try:
                        row_id = str(getattr(pos, "row_id", "") or "")
                        if not row_id:
                            rows = db.select("paper_trades",
                                             filters={"ticket": str(pos.ticket or "")},
                                             limit=1)
                            row_id = str(rows[0].get("id") or "") if rows else ""
                        if row_id:
                            patch = {"partial_done": True,
                                     "volume": remaining_vol}
                            init_vol = _resolve_initial_volume(db, pos)
                            if init_vol is not None:
                                patch["initial_volume"] = init_vol
                            db.update("paper_trades", row_id, patch)
                    except Exception as exc:
                        log.debug("partial_done persist failed: %s", exc)
                    # Realized PnL of the CLOSED SLICE only (in USD) — the
                    # remaining lots keep their own unrealized PnL. Computed
                    # on a slice-volume view so the journal/timeline shows the
                    # money actually banked by this scale-out.
                    slice_pnl = execution.PaperBrokerPnl.compute(
                        SimpleNamespace(
                            direction=pos.direction, asset=pos.asset,
                            entry_price=pos.entry_price,
                            current_price=price, volume=slice_vol),
                        s, asset=str(pos.asset or ""), rates=rates)
                    signal_log.log_event(
                        db=db, event="closed", asset=str(pos.asset or ""),
                        direction=str(pos.direction or ""),
                        entry=pos.entry_price, exit_price=price,
                        ticket=str(pos.ticket or ""), volume=slice_vol,
                        pnl=slice_pnl, source="auto",
                        reason=f"ปิดบางส่วน (TP1) {slice_vol:g} lots "
                               f"ที่ {price:g} — ที่เหลือ trailing")
                    try:
                        await notifier.notify(
                            pos.user_id, "trade_closed",
                            f"💰 Partial Close (TP1)\n"
                            f"Asset: {pos.asset}\nDirection: {pos.direction}\n"
                            f"Closed: {slice_vol:g} lots @ {price:g}\n"
                            f"PnL: {_pnl_text(slice_pnl)}\n"
                            f"Remaining: {pos.volume:g} lots (trailing)")
                    except Exception as exc:
                        log.debug("partial notify failed: %s", exc)
            except Exception as exc:
                log.warning("partial close %s failed: %s", pos.ticket, exc)

    # ---- 2. breakeven + trailing (+ R-ladder floor when enabled) ---------
    # Market closed → no SL move either (owner 2026-09-19, final): the real
    # broker cannot modify a stop while the market is shut, and the frozen
    # mark makes the R-multiple meaningless. Deferred to the first open cycle.
    new_sl: float | None = None
    if discretionary_block:
        return out
    if be_trigger > 0 and r_multiple >= be_trigger:
        be_price = pos.entry_price
        if trail_mult > 0:
            atr = _atr_for(pos, r_distance, db,
                           proxy_mult=float(G(s, "guard_atr_proxy_mult", zero_as_missing=False) or 0)
                           or None)
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
                if bool(G(s, "trailing_ladder")):
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
                    # SL move LINE alert — sl_moved is CRITICAL so it pushes
                    # immediately (honours the notify_stop_loss switch inside
                    # NotificationService.notify, which also throttles it per
                    # asset so a ratcheting trail can't spam LINE). Fail-soft:
                    # never break guard.
                    try:
                        await notifier.notify(
                            pos.user_id, "sl_moved",
                            f"🔔 SL ขยับ ({move_kind})\n"
                            f"Asset: {pos.asset}\nDirection: {pos.direction}\n"
                            f"SL {old_sl:g} → {pos.stop_loss:g} @ {price:g} "
                            f"({r_multiple:.2f}R)\nTicket {pos.ticket}",
                            asset=str(pos.asset or ""),
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
    emergency_held = 0
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
                "emergency_held": 0,
                "sl_assets": "", "closed_assets": "", "skip_assets": ""}

    # Settings once per cycle (breakeven/trailing/partial knobs). Falls back
    # to schema defaults when the DB is unavailable — EXCEPT that safety
    # decisions need to KNOW whether the read really succeeded. ``settings_ok``
    # records that, and the emergency evaluation refuses to quote a breach
    # against a guessed limit when it is False (prod 2026-09-22: a swallowed
    # settings read gave the kill switch the default 10% instead of 15% and
    # closed 6 positions at 13.25% drawdown — see execution.evaluate_kill).
    s = settings
    settings_ok = settings is not None
    if s is None:
        loaded = execution.settings_or_none(db)
        if loaded is not None:
            s = loaded
            settings_ok = True
        else:
            from app.models.schemas import AppSettings
            s = AppSettings()
            settings_ok = False

    assets = sorted({str(p.asset or "").upper() for p in positions})

    # ---- Smart Exit shared context (once per cycle, fail-safe) ------------
    # EXIT PRIORITY 1-8: emergency → SL/TP → trailing(ladder) → AI score →
    # reversal → time → news. SL/TP/trailing/time live in this loop; the AI
    # engine supplies score/reversal/news/left-behind/volatility/profit.
    smart_on = bool(G(s, "smart_exit_enabled"))
    # ---- P1-2: position_management_mode gates DISCRETIONARY management -----
    # "auto" = full guard. "protective_only" / "advisory" = NO discretionary
    # management (partial / breakeven / trailing / R-ladder / smart-exit /
    # time stop). Hard SL/TP + Emergency Exit REMAIN active in every mode —
    # a hard stop is a safety INVARIANT, never something the guard may drop.
    mgmt_discretionary = bool(s.management_allows_discretionary())
    # ---- Market-closed gate for ALL closes (once per cycle) ---------------
    # Owner rule (2026-09-19, final): "ห้ามปิดด้วยเพราะว่าในแอพจริงปิดไม่ได้
    # เช่นกัน และจะไม่เกิดตอนตลาดปิดเพราะราคาจะนิ่ง". While the market is
    # shut NOTHING closes — not Smart Exit, not the time stop, not TP1, and
    # not even SL/TP or the Emergency Exit: a frozen mark cannot legitimately
    # trigger a stop, and the real broker cannot fill a close either.
    # Computed ONCE per cycle (the clock cannot change mid-loop) and
    # fail-CLOSED: an unreadable clock blocks every close.
    discretionary_block = execution.market_closed_discretionary_close_block()
    if discretionary_block:
        log.info("position guard: all closes blocked — %s",
                 discretionary_block)
    snaps: dict[str, dict] = {}
    news_status, news_event = "SAFE", ""
    avg_hold = S("avg_hold_fallback_days")
    drawdown_pct = 0.0
    kill_engaged = False
    kill_triggers: list[str] = []
    # Priority 1 — Emergency Exit runs even when Smart Exit is OFF: the kill
    # switch is a safety path, not an AI feature. Single shared path
    # (execution.evaluate_kill) — same math as the entry gate and the
    # monitor banner, can never drift apart.
    if positions:
        try:
            ks = execution.evaluate_kill(db, s, settings_confirmed=settings_ok)
            kill_engaged = bool(getattr(ks, "engaged", False))
            kill_triggers = list(getattr(ks, "triggers", []) or [])
        except Exception as exc:
            log.debug("emergency kill check failed: %s", exc)
            kill_engaged = False

    # ---- Emergency exit HOLD: the owner is still being asked ---------------
    # Prod 2026-09-14: the monitor pushed the "confirm the +5% expansion"
    # prompt at 17:33 and THIS worker emergency-closed AUDNZD ~6 s later — the
    # owner never got to press Approve, even though the very message in their
    # hand says "ลิมิตยังไม่ถูกแตะต้อง". While an unanswered request is on the
    # table the exit is DEFERRED (not disabled): positions keep their SL/TP
    # management from the normal pass below, and the pause stays engaged so no
    # new order can be opened.
    #
    # Owner: "ต้องรอคอมเฟิร์มก่อนถึงจะ kill switch ทำงาน" + "ถ้ารอตาม
    # kill_expand_ttl_min แล้วไม่ได้รับการตอบกลับให้ขยายอัตโนมัติ" → the wait IS
    # the confirmation window (no fixed cap, no grace), and when it runs out the
    # TIMEOUT POLICY decides — not the guard. No answer means the +5% expansion
    # is applied (owner decision), which normally clears the breach; a window
    # that CANNOT be applied because the settings write keeps failing has not
    # been served either, so the hold simply continues (see ``settle.holds``).
    # With the one-shot policy ON-OFF ("ขยายอัตโนมัติ 1 ครั้ง", migration 039) the
    # SECOND silence is ``capped``: the policy answered it already, so the guard
    # closes as usual ("ขยายแล้วยังไม่พอ = ปิดไม้ทันที").
    hold_row = None
    hold_note = ""
    # Did a request already exist this cycle (held, or lapsed-and-settled)? If
    # so the owner HAS been asked and their silence has been answered by the
    # timeout policy — the guard must not re-arm a fresh prompt, it closes as
    # the owner decided ("ขยายแล้วยังไม่พอ = ปิดไม้ทันที"). A fresh prompt is
    # only raised when NO request was ever on the table (the 2026-09-22 gap).
    request_known = False
    if kill_engaged:
        hold_row = limit_expand.emergency_hold(db, s)
        if hold_row is not None:
            request_known = True
            kill_engaged = False
            emergency_held = len(positions)
            log.warning(
                "emergency exit HELD for %d position(s): kill switch is "
                "engaged (%s) but request %s is still awaiting the owner "
                "(%.0f min of %.0f)",
                emergency_held, "; ".join(kill_triggers)[:120] or "engaged",
                hold_row.get("id"), limit_expand.pending_age_min(hold_row),
                limit_expand.ttl_minutes(s))
        else:
            # No request is waiting for an answer any more. If one ran out of
            # time, apply the expansion the owner asked for and re-judge the
            # account against the WIDENED limits before closing anything: the
            # monitor normally settles the lapsed window first, and doing it
            # here too stops the guard from closing a book that the owner's own
            # policy was about to rescue. A window that cannot be settled keeps
            # the hold — see ``settle.holds``.
            try:
                stale = limit_expand.stale_pending(db, s)
            except Exception:
                stale = None
            settle = None
            if stale is not None:
                request_known = True
                try:
                    settle, _pushed = limit_expand.settle_lapsed_window(
                        db, s, notifier)
                except Exception as exc:
                    log.error("auto-expand of the lapsed window failed: %s", exc)
                if settle is not None and settle.settled:
                    reloaded = execution.settings_or_none(db)
                    if reloaded is not None:
                        s = reloaded
                        settings_ok = True
                    else:
                        log.debug("settings reload after auto-expand failed — "
                                  "keeping prior limits")
                    try:
                        ks = execution.evaluate_kill(
                            db, s, settings_confirmed=settings_ok)
                        kill_engaged = bool(getattr(ks, "engaged", False))
                        kill_triggers = list(getattr(ks, "triggers", []) or [])
                    except Exception as exc:
                        log.debug("kill re-check after auto-expand failed: %s",
                                  exc)
                    if not kill_engaged:
                        emergency_held = len(positions)
                        log.warning(
                            "emergency exit NOT needed: the lapsed window was "
                            "auto-applied (%d position(s) stay open)",
                            emergency_held)
                if settle is not None and settle.holds:
                    # The window could NOT be settled: the settings write did not
                    # land, so the owner's approval is still being honoured and
                    # retried every cycle. Owner decision 2026-09-14: "ถ้าเขียน
                    # DB ไม่สำเร็จห้ามปิดไม้" → keep DEFERRING instead of closing:
                    # the row stays open, so the LINE buttons and the popup still
                    # work, SL/TP keeps protecting the positions, and
                    # settle_lapsed_window has already warned the owner (once per
                    # 6 h, the first time immediately). A close here would be an
                    # irreversible answer to a question the owner never lost.
                    # NOTE: a window the one-shot policy refused (``capped``, the
                    # auto-expand quota is used up) does NOT hold — that silence
                    # has been answered already, so the guard closes below.
                    kill_engaged = False
                    emergency_held = len(positions)
                    log.warning(
                        "emergency exit HELD: request %s lapsed %.0f min ago "
                        "but could not be settled (%s) — retrying next cycle "
                        "instead of closing %d position(s)",
                        stale.get("id"), limit_expand.pending_age_min(stale),
                        settle.kind, emergency_held)
                if kill_engaged:
                    # Still over the WIDENED limit (or the row could not even be
                    # dated, so there was nothing to settle). Say WHICH outcome
                    # it was in the close message: the owner may still be
                    # looking at a prompt that promised nothing would be
                    # touched, and "ขยายให้แล้ว" would be a lie for a window that
                    # was skipped or never applied.
                    age = limit_expand.pending_age_min(stale)
                    ttl = limit_expand.ttl_minutes(s)
                    kind = getattr(settle, "kind", "none") if settle else "none"
                    if kind == "applied":
                        head = (f"⏳ ไม่มีการยืนยันภายใน {ttl:.0f} นาที → "
                                "ขยายลิมิตให้อัตโนมัติแล้ว แต่ยังเกินลิมิตใหม่")
                    elif kind == "skipped":
                        head = (f"⏳ ครบช่วงยืนยัน {ttl:.0f} นาที — "
                                "คำขอไม่ต้องขยายอีกแล้ว (ลิมิตปัจจุบันสูงกว่าที่"
                                "คำขอเสนอ) แต่ยังเกินลิมิตอยู่")
                    elif kind == "no-breach":
                        head = (f"⏳ ครบช่วงยืนยัน {ttl:.0f} นาที — "
                                "ไม่มีลิมิตที่เกินอยู่แล้ว จึงไม่ขยายลิมิต "
                                "แต่ kill switch ยังเข้าเงื่อนไขอยู่")
                    elif kind == "retired":
                        head = (f"⏳ ครบช่วงยืนยัน {ttl:.0f} นาที — คำขอไม่มี"
                                "ลิมิตให้ขยาย (ยกเลิกคำขอแล้ว) แต่ยังเกินลิมิตอยู่")
                    elif kind == "capped":
                        head = (f"⏳ ไม่มีการยืนยันภายใน {ttl:.0f} นาที — "
                                "ระบบขยายให้เองได้ครั้งเดียว (นโยบายปิด) "
                                "จึงไม่ขยายให้อีก และยังเกินลิมิตเดิมอยู่")
                    else:
                        head = (f"⏳ คำขอยืนยันยังไม่ถูกตอบมา {age:.0f} นาที "
                                f"(เลยช่วงยืนยัน {ttl:.0f} นาที) "
                                "และขยายอัตโนมัติไม่สำเร็จ")
                    hold_note = (head + "\nจึงปิดไม้เพื่อความปลอดภัย — "
                                 "ยังกดอนุมัติได้ แต่จะไม่หยุดการปิดไม้อีก")
                    log.warning(
                        "emergency exit resumed: request %s unanswered for "
                        "%.0f min (settle=%s)", stale.get("id"), age, kind)

    # ---- Confirmation gate: NEVER close without first asking ---------------
    # Owner rule (2026-09-22): "ต้องแจ้งเตือนแล้วรอ user confirm ก่อนตามระบบ
    # ก่อนหน้านี้". The prompt used to be created ONLY by the monitor's breach
    # branch, which uses a DIFFERENT drawdown definition (PortfolioSnapshot /
    # RiskEngine) than this guard (evaluate_kill / equity_snapshots). On
    # 2026-09-22 those two disagreed — the monitor saw 13.25% < 15% ("no
    # breach") while the guard saw 13.25% > 10% (a swallowed settings read had
    # left it with the DEFAULT limit) — so no request ever existed,
    # ``emergency_hold`` returned None, and the guard closed 6 positions with
    # no prompt. The guard is now its OWN entry point into the prompt flow: if
    # it is engaged and nothing is awaiting an answer, it ASKS FIRST (creates
    # the request + pushes Approve/Reject) and DEFERS this cycle. It closes
    # only after the owner's confirmation window resolves (approve/reject or
    # the timeout policy above) — same as when the monitor raised the request.
    # When a request ALREADY existed this cycle (``request_known``) the owner
    # has been asked: the timeout policy owns the outcome, so no fresh prompt
    # is raised and the guard closes exactly as before.
    if kill_engaged and not request_known:
        if not settings_ok:
            # Limits could not be read, so we cannot QUOTE a breach at all —
            # raise no prompt (it would quote the default 10%) and defer: the
            # next cycle retries the settings read. This is the fail-safe
            # response to 2026-09-22, where a swallowed read made the guard
            # close against the default 10% instead of the configured 15%.
            kill_engaged = False
            emergency_held = len(positions)
            log.error(
                "emergency exit HELD for %d position(s): kill switch could "
                "not read the owner's settings — refusing to act on guessed "
                "limits (triggers: %s)",
                emergency_held, "; ".join(kill_triggers)[:120] or "engaged")
        else:
            try:
                created = limit_expand.request_and_notify(
                    db, s, notifier, source="guard")
            except Exception as exc:
                created = {"notified": False, "reason": "error"}
                log.error("guard could not raise the limit-expand prompt: %s",
                          exc)
            # Only defer when a request is genuinely on the table AND it can be
            # dated: just created (fresh), or already pending with a readable
            # window. An UNDATEABLE row must not read as "brand new" (owner
            # decision: an age we cannot measure must not stall the exit —
            # see test_an_undateable_request_means_close_not_hold). Everything
            # else means the owner's silence was already answered and the close
            # proceeds: ``cooldown``/``auto_applied``/``auto_capped``/
            # ``no_breach`` from the timeout policy, and ``insert_failed`` when
            # the request table itself is broken (fail-safe is CLOSE there).
            reason = (created or {}).get("reason")
            row = (created or {}).get("request")
            datable = row is not None and (
                row.get("requested_at") or row.get("created_at"))
            still_pending = (bool((created or {}).get("requested")) and datable) \
                or (reason == "already_pending" and datable)
            if still_pending:
                kill_engaged = False
                emergency_held = len(positions)
                log.warning(
                    "emergency exit HELD for %d position(s): kill switch is "
                    "engaged (%s) but no confirmation was outstanding — raised "
                    "a fresh request (%s) and will wait for the owner",
                    emergency_held, "; ".join(kill_triggers)[:120] or "engaged",
                    reason)

    # ---- Feed phase: live marks + snapshots + news IN PARALLEL -----------
    # These are independent fetches — running them one after another cost
    # their SUM (~55s) and overran the 1-min interval (see _GUARD_*_BUDGET).
    # Each branch keeps its own cap, so a slow snapshot/news feed degrades
    # Smart Exit to a blind HOLD but can never delay — let alone drop — the
    # live marks that drive the SL/TP safety path.
    live: dict[str, float] = {}
    if positions:
        # LIVE Settings values (guard_*_timeout_s) — the module aliases are
        # import-time fallback only.
        _marks_c = float(G(s, "guard_marks_timeout_s", zero_as_missing=False) or _GUARD_MARKS_BUDGET)
        _snaps_c = float(G(s, "guard_snap_timeout_s", zero_as_missing=False) or _GUARD_SNAP_BUDGET)
        _news_c = float(G(s, "guard_news_timeout_s", zero_as_missing=False) or _GUARD_NEWS_BUDGET)
        marks_c, snaps_c, news_c = await asyncio.gather(
            _bounded(_live_marks(assets), _marks_c),
            (_bounded(quotes.fetch_all_snapshots(assets), _snaps_c)
             if smart_on else _none()),
            (_bounded(_smart_exit_news(db, s), _news_c)
             if smart_on else _none()),
        )
        live = marks_c or {}
        snaps = snaps_c or {}
        if news_c:
            news_status, news_event = news_c
    # Quote→USD conversion map for every journaled PnL this cycle. Built from
    # the marks in hand plus a one-shot fetch of the missing <quote>USD legs.
    pnl_rates: dict[str, float] = (
        await _bounded(_conversion_rates(positions, live), float(G(s, "guard_marks_timeout_s", zero_as_missing=False) or _GUARD_MARKS_BUDGET))
        if positions else {}
    ) or {}
    # Publish the map on the broker so sibling workers (portfolio_monitor's
    # equity snapshot) convert with the SAME rates instead of re-fetching.
    try:
        broker.set_rates(pnl_rates)
    except Exception as exc:
        log.debug("broker.set_rates failed: %s", exc)
    if smart_on and positions:
        try:
            avg_hold = _avg_hold_days(db)
        except Exception:
            avg_hold = S("avg_hold_fallback_days")
        try:
            drawdown_pct = execution.equity_drawdown_pct(
                db, float(G(s, "capital", zero_as_missing=False) or 0))
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
        # ``kill_engaged`` is already False when the exit is on HOLD (an
        # unanswered confirmation request) — then the position falls through
        # to the normal management pass, so SL/TP keep protecting it.
        #
        # Market closed → the emergency exit is ALSO blocked (owner
        # 2026-09-19, final): the real broker cannot fill a close while the
        # market is shut, so flattening here would only book a fake PnL on a
        # frozen mark. It fires on the first open cycle instead.
        if kill_engaged and discretionary_block:
            emergency_held = len(positions)
            skip_assets.append(f"{pos.asset}:kill_market_closed")
            log.info("emergency exit held %s: market closed", pos.ticket)
            continue
        if kill_engaged:
            # Self-diagnosing close (prod 2026-09-24: 3 closes fired while 3
            # fresh pendings were on the table and the cycle logs were already
            # purged, so the branch could not be reconstructed). Quote the
            # hold-state AT CLOSE TIME into the journal reason + server log.
            try:
                _diag = (f" [hold-state: pending={limit_expand.pending_count(db)}"
                         f" request_known={request_known}"
                         f" settings_ok={settings_ok}]")
            except Exception:
                _diag = " [hold-state: unreadable]"
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
                pos, s, asset=str(pos.asset or ""), rates=pnl_rates)
            execution.close_trade_rows(db, pos.ticket, price, pnl, "emergency",
                                       asset=str(pos.asset or ""),
                                       direction=str(pos.direction or ""))
            signal_log.log_event(
                db=db, event="closed", asset=str(pos.asset or ""),
                direction=str(pos.direction or ""),
                entry=pos.entry_price, exit_price=price, pnl=pnl,
                ticket=str(pos.ticket or ""), source="auto",
                reason="🚨 Emergency Exit (kill switch: "
                       + ("; ".join(kill_triggers)[:200] or "engaged")
                       + f") — ปิดที่ {price:g}" + _diag)
            emergency_closed += 1
            closed += 1
            log.warning("EMERGENCY CLOSE %s %s%s", pos.ticket, pos.asset, _diag)
            closed_assets.append(f"{pos.asset}:kill")
            emergency_pnl += float(pnl or 0)
            emergency_user = emergency_user or str(getattr(pos, "user_id", "") or "")
            emergency_lines.append(
                f"• {pos.asset} {pos.direction} "
                f"{pos.entry_price:g}→{price:g} PnL {_pnl_text(pnl)}")
            continue

        # ---- Priorities 2-3: compute HARD SL/TP BEFORE any discretionary
        # management. P1-1: a hard stop is TERMINAL and must win over every
        # softer rule. Computing it here (instead of after the management
        # pass) means a cycle that already hit SL or TP will NOT partial-close
        # / breakeven / trail the position first — the position closes at the
        # touched level with no side effects on a position about to be gone.
        sl, tp = pos.stop_loss, pos.take_profit
        hit_sl = sl is not None and (
            (pos.direction == "BUY" and price <= sl)
            or (pos.direction == "SELL" and price >= sl))
        hit_tp = tp is not None and (
            (pos.direction == "BUY" and price >= tp)
            or (pos.direction == "SELL" and price <= tp))

        # ---- management pass: breakeven / trailing / partial (TP1) ----
        # P1-1: skipped entirely when a hard SL/TP is touched this cycle, so
        # management can never mutate (or partially close) a position the
        # hard-stop block will close right after. Not-hit cycles are unchanged.
        # P1-2: also skipped when position_management_mode disables
        # discretionary management (protective_only / advisory).
        if mgmt_discretionary and not (hit_sl or hit_tp):
            try:
                mgmt = await _manage_position(db, broker, pos, price, s, notifier,
                                              discretionary_block,
                                              rates=pnl_rates)
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

        if hit_sl or hit_tp:
            pass  # handled by the SL/TP close block after smart-exit skip
        elif discretionary_block:
            # Market closed: NOTHING closes — not even a hard stop. Skip the
            # whole evaluation (no snapshot fetch, no broker call) and record
            # WHY so the Guard tab can explain an untouched position.
            smart_skipped += 1
            skip_assets.append(f"{pos.asset}:market_closed")
        elif not mgmt_discretionary:
            # P1-2: position_management_mode = protective_only / advisory →
            # Smart Exit (AI score / reversal / news / left-behind) is
            # discretionary and stays OFF. Hard SL/TP above still protect.
            smart_skipped += 1
            skip_assets.append(f"{pos.asset}:mgmt_off")
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
                            db, broker, pos, price, decision, s, notifier,
                            rates=pnl_rates)
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
            #
            # Market closed → the time stop is ALSO a discretionary exit
            # (owner 2026-09-19): it books a PnL on a stale mark, so it waits
            # for the reopen exactly like Smart Exit. The position simply
            # ages one more weekend; SL/TP still protect it meanwhile.
            max_hold = int(G(s, "max_hold_days", zero_as_missing=False) or 0)
            if not mgmt_discretionary:
                # P1-2: the time stop is discretionary → OFF in protective_only
                # / advisory. Hard SL/TP still apply.
                if max_hold > 0:
                    skip_assets.append(f"{pos.asset}:time_mgmt_off")
            elif max_hold > 0 and discretionary_block:
                skip_assets.append(f"{pos.asset}:time_market_closed")
                log.info("time stop held %s: market closed", pos.ticket)
            elif max_hold > 0:
                age_days = _position_age_days(pos, db)
                # R-exemption (2026-09-11, option "ก"): age alone is not a
                # reason to cut a live winner. The left_behind rule above is
                # R-conditional and spares profitable positions; without this
                # check the R-BLIND time stop re-closed exactly what
                # left_behind let through — a +3R position could be killed on
                # day 5. R uses the ORIGINAL stop so a trailed/breakeven SL
                # can't inflate it (see _r_multiple_at).
                ts_min_r = float(G(s, "time_stop_min_r", zero_as_missing=False) or 0)
                r_now = _r_multiple_at(pos, price, db) if ts_min_r > 0 else 0.0
                if age_days >= max_hold and (ts_min_r <= 0 or r_now < ts_min_r):
                    age_txt = f"{age_days:.1f}"
                    result = await broker.close_position(pos.ticket)
                    if not result.ok:
                        log.warning("time-stop close %s failed: %s",
                                    pos.ticket, result.message)
                        continue
                    pnl = execution.PaperBrokerPnl.compute(
                        pos, s, asset=str(pos.asset or ""), rates=pnl_rates)
                    execution.close_trade_rows(db, pos.ticket, price, pnl, "time",
                                               asset=str(pos.asset or ""),
                                               direction=str(pos.direction or ""))
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
                            f"PnL: {_pnl_text(pnl)}",
                        )
                    except Exception as exc:
                        log.error("time-stop notify failed: %s", exc)
                elif age_days >= max_hold:
                    log.info("time stop spared %s: %+.2fR ≥ %.2gR after %.1f days",
                             pos.ticket, r_now, ts_min_r, age_days)
            continue

        reason = "sl" if hit_sl else "tp"
        # Market closed → even a hard stop cannot fire (owner 2026-09-19,
        # final): the mark is frozen, so a "hit" is an artefact of a stale
        # price, not a real market event, and the real broker cannot fill the
        # close anyway. Deferred to the first open cycle.
        if discretionary_block:
            skip_assets.append(f"{pos.asset}:{reason}_market_closed")
            log.info("%s hit %s held: market closed", reason, pos.ticket)
            continue
        result = await broker.close_position(pos.ticket)
        if not result.ok:
            log.warning("close %s failed: %s", pos.ticket, result.message)
            continue

        pnl = execution.PaperBrokerPnl.compute(
            pos, s, asset=str(pos.asset or ""), rates=pnl_rates)
        execution.close_trade_rows(db, pos.ticket, price, pnl, reason,
                                   asset=str(pos.asset or ""),
                                   direction=str(pos.direction or ""))
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
                f"PnL: {_pnl_text(pnl)}",
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
                "🚨 Emergency Exit (KILL SWITCH) — ปิด "
                + f"{emergency_closed} ไม้\n"
                + body + "\n"
                + (hold_note + "\n" if hold_note else "")
                + f"รวม PnL {emergency_pnl:+,.2f}\n"
                + ("; ".join(kill_triggers)[:200] or "kill switch engaged"),
            )
        except Exception as exc:
            log.error("emergency notify failed: %s", exc)

    return {"checked": len(positions), "closed": closed,
            "moved_sl": moved, "partial_closed": partials,
            "smart_closed": smart_closed, "smart_partials": smart_partials,
            "smart_skipped": smart_skipped,
            "emergency_closed": emergency_closed,
            # >0 = the kill switch was engaged but an unanswered confirmation
            # request deferred the emergency exit (see limit_expand).
            "emergency_held": emergency_held,
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
            # Original size (migration 048) — carried so a later partial can
            # persist it for legacy rows and the UI can show closed/original.
            if row.get("initial_volume") is not None:
                book[ticket].initial_volume = float(row["initial_volume"])  # type: ignore[attr-defined]
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


def seed_order_sequence(db, broker) -> int:
    """Push the in-memory order sequence past every ticket the DB remembers.

    WHY: `PaperBroker._seq` starts at 0 on every construction, so a restart
    re-issues ticket numbers that PERMANENT tables still hold. rehydrate_book
    only walks past tickets of rows that are still OPEN — after a stats reset
    (which deletes closed rows) the sequence fell back to 1 and the next trade
    took `PAPER-000001`, the ticket the AUDNZD trade had closed with an hour
    earlier (prod 2026-09-14 11:10Z). The monitor groups its SL/TP timeline by
    ticket, so the still-open AUDCHF row showed the old AUDNZD close as its own.

    Sources, both already consulted by the UI:
      * paper_trades — the journal (closed rows live until a stats reset),
      * signal_logs — the audit log that feeds the timeline (7-day TTL).

    Only when BOTH have forgotten a number is it reused — and then there is no
    history left to show under the wrong symbol. Never raises; returns the
    sequence in use.
    """
    if not hasattr(broker, "_seq"):
        return 0
    cur = int(getattr(broker, "_seq", 0) or 0)
    best = cur
    for table in ("paper_trades", "signal_logs"):
        getter = getattr(db, "max_ticket", None)
        if not callable(getter):        # old fakes / other brokers
            continue
        try:
            best = max(best, int(getter(table) or 0))
        except Exception as exc:        # pragma: no cover - defensive
            log.warning("seed: max_ticket(%s) failed: %s", table, exc)
    if best != cur:
        log.info("seed: order sequence %d → %d (tickets already in the DB)",
                 cur, best)
    broker._seq = best
    return best
