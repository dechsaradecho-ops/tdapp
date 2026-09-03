"""Worker #6 — Position Guard (every 1 min).

SL/TP enforcement loop for the PaperBroker: walks every open position, marks it
to the latest price (live quote when available), closes it when the stop-loss or
take-profit is touched, journals the close into paper_trades (so the kill
switch / frequency engines see realized PnL) and notifies the user.

Real broker adapters (MT5/OANDA) enforce SL/TP server-side; their close events
still flow through close_trade_rows so the journal stays authoritative.
"""
from __future__ import annotations

import asyncio
import logging

from app.integrations import quotes
from app.integrations.brokers import Position
from app.services import execution
from app.services.notification_service import NotificationService

log = logging.getLogger(__name__)


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


async def guard_once(db, broker, notifier: NotificationService) -> dict:
    """One guard cycle. Returns a small summary for logs/tests."""
    closed = 0
    try:
        positions = await broker.all_positions()
    except Exception as exc:
        log.error("position guard cannot list positions: %s", exc)
        return {"checked": 0, "closed": 0}

    # One batched live-mark fetch per cycle (30s feed cache keeps it cheap)
    live = await _live_marks(sorted({p.asset.upper() for p in positions})) \
        if positions else {}

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

        sl, tp = pos.stop_loss, pos.take_profit
        if sl is None and tp is None:
            continue

        hit_sl = sl is not None and (
            (pos.direction == "BUY" and price <= sl)
            or (pos.direction == "SELL" and price >= sl))
        hit_tp = tp is not None and (
            (pos.direction == "BUY" and price >= tp)
            or (pos.direction == "SELL" and price <= tp))
        if not (hit_sl or hit_tp):
            continue

        reason = "sl" if hit_sl else "tp"
        result = await broker.close_position(pos.ticket)
        if not result.ok:
            log.warning("close %s failed: %s", pos.ticket, result.message)
            continue

        pnl = execution.PaperBrokerPnl.compute(pos)
        execution.close_trade_rows(db, pos.ticket, price, pnl, reason)
        closed += 1
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

    return {"checked": len(positions), "closed": closed}


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
    restored = 0
    for row in rows or []:
        ticket = str(row.get("ticket") or "")
        if not ticket or ticket in book:
            continue
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
            restored += 1
        except Exception as exc:
            log.warning("rehydrate: skip %s: %s", ticket or "?", exc)
    if restored:
        log.info("rehydrate: restored %d open position(s) into the broker book", restored)
    return restored
