"""Goal Engine endpoint — assess feasibility of a monthly return target.

Reality-aware (2026-09-04, synced 2026-09-11): the assessment folds in the
user's LIVE trading state — realized PnL + win rate from closed paper_trades
(paged, never truncated at 500), unrealized PnL + equity + drawdown
(monitor parity), the current market regime from market_analysis using the
TOP-SCORING asset (same source as the market header + chat context — never
the newest row), the kill switch state and the manual pause switch. Every
read is fail-safe: a broken DB or empty tables degrade to the pure envelope
math, never raise.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from app.engine.goal_engine import GoalEngine
from app.models.schemas import GoalAssessment, GoalInput, GoalRealityContext

router = APIRouter()
engine = GoalEngine()


def _paper_rows(db) -> list[dict]:
    """All paper_trades rows — paged (PostgREST caps one request at 1000).

    select_paged walks pages; plain select(limit=500) silently truncated the
    journal once the table grew (same class as the logs row-cap fix). Falls
    back to select() when the DB handle has no pager (very old fakes).
    """
    pager = getattr(db, "select_paged", None)
    if callable(pager):
        try:
            return list(pager("paper_trades", order="created_at",
                              desc=True, page_size=1000, max_rows=20000) or [])
        except Exception:
            pass
    try:
        return list(db.select("paper_trades", limit=500) or [])
    except Exception:
        return []


def _parse_dt(raw) -> datetime | None:
    try:
        from app.services.execution import _parse_dt as _pdt
        return _pdt(raw)
    except Exception:
        pass
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def _reality_from_db(db, broker=None) -> GoalRealityContext:
    """Collect live portfolio + market state (all reads fail-safe → defaults)."""
    ctx = GoalRealityContext()

    if not db or not getattr(db, "available", False):
        return ctx

    # ---- single settings load (single source of truth — see chat.py) -----
    settings = None
    try:
        from app.api.routes.settings import get_app_settings
        settings = get_app_settings(db)
    except Exception:
        settings = None
    cap = float(getattr(settings, "capital", 0) or 0) if settings else 0.0
    ctx.settings_capital = round(cap, 2)
    if settings is not None:
        try:
            ctx.order_mode = str(getattr(settings, "order_mode", "auto") or "auto")
            ctx.risk_per_trade_pct = float(
                getattr(settings, "risk_per_trade_pct", 0) or 0)
            ctx.allowed_assets = list(
                getattr(settings, "effective_assets", lambda: [])() or [])
        except Exception:
            pass

    # ---- 1) Portfolio stats from paper_trades (same math as monitor) ------
    open_rows: list[dict] = []
    try:
        rows = _paper_rows(db)
        closed = [r for r in rows
                  if r.get("status") == "closed" and r.get("pnl") is not None]
        wins = [r for r in closed if float(r.get("pnl") or 0) > 0]
        ctx.closed_count = len(closed)
        ctx.pnl_total = round(sum(float(r.get("pnl") or 0) for r in closed), 2)
        ctx.win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
        open_rows = [r for r in rows if r.get("status") == "open"]
        ctx.open_positions = len(open_rows)
        ctx.data_available = bool(closed) or bool(open_rows)
        # frequency (today/week) — same created_at windows as monitor stats
        try:
            now = datetime.now(timezone.utc)
            today = now.date().isoformat()
            week_ago = now - timedelta(days=7)
            ctx.trades_today = len(
                [r for r in rows
                 if (_parse_dt(r.get("created_at")) or now).date().isoformat() == today
                 and _parse_dt(r.get("created_at")) is not None])
            ctx.trades_week = len(
                [r for r in rows if (_parse_dt(r.get("created_at")) or now) >= week_ago
                 and r.get("status") != "rejected"
                 and _parse_dt(r.get("created_at")) is not None])
        except Exception:
            pass
    except Exception:
        pass  # stats unavailable → envelope math only

    # ---- 2) Market regime — TOP SCORER (same as market header + chat) -----
    # One scanner cycle writes ~28 rows, so limit=50 keeps the whole cycle;
    # the old limit=25 truncated the tail and analysis[0] (newest row = an
    # arbitrary asset) disagreed with the home header + chat context.
    try:
        analysis = db.select("market_analysis", limit=50)
        seen: set[str] = set()
        per_asset: dict[str, tuple[float, str, str]] = {}
        for row in analysis or []:
            asset = str(row.get("asset") or "")
            if asset in seen:
                continue  # select() is newest-first → keep newest row per asset
            seen.add(asset)
            try:
                score = float(row.get("confidence") or 0)
            except Exception:
                score = 0.0
            per_asset[asset] = (
                score,
                str(row.get("regime") or "sideway"),
                str(row.get("sentiment") or "neutral"),
            )
        if per_asset:
            top_asset, (top_score, top_regime, top_sent) = max(
                per_asset.items(), key=lambda kv: kv[1][0])
            ctx.market_regime = top_regime or "sideway"
            ctx.market_sentiment = top_sent or "neutral"
            ctx.top_asset = top_asset
            ctx.top_score = round(float(top_score or 0), 1)
            ctx.data_available = True
    except Exception:
        pass

    # ---- 3) Kill switch (single shared path — see evaluate_kill) ---------
    try:
        from app.services.execution import evaluate_kill

        s = settings
        if s is None:
            from app.api.routes.settings import get_app_settings as _gas
            s = _gas(db)
        ks = evaluate_kill(db, s)
        ctx.kill_switch_engaged = ks.engaged
        ctx.kill_triggers = list(ks.triggers)
    except Exception:
        pass  # kill state unknown → treat as clear (fail-open for assessment only)

    # ---- 4) Manual pause switch ------------------------------------------
    try:
        from app.services.execution import get_pause
        pause = get_pause(db)
        ctx.trading_paused = pause.paused
        ctx.pause_reason = pause.reason or ""
    except Exception:
        pass

    # ---- 5) Unrealized + equity + drawdown (monitor parity, best-effort) --
    try:
        unreal = 0.0
        if open_rows:
            marks: dict[str, float] = {}
            if broker is not None:
                try:
                    if hasattr(broker, "all_positions"):
                        positions = broker.all_positions()
                        if inspect.iscoroutine(positions):
                            positions = await positions
                        else:
                            positions = list(positions or [])
                    else:
                        positions = []
                    for pos in positions or []:
                        price = getattr(pos, "current_price", None)
                        if not price and hasattr(broker, "mark_price"):
                            try:
                                price = broker.mark_price(pos.ticket)
                                if inspect.iscoroutine(price):
                                    price = await price
                            except Exception:
                                price = None
                        if price:
                            try:
                                marks[str(pos.ticket)] = float(price)
                                marks.setdefault(
                                    "asset:" + str(getattr(pos, "asset", "")).upper(),
                                    float(price))
                            except Exception:
                                pass
                except Exception:
                    pass
            # live spot feed tops up what the broker book missed (same
            # priority as monitor: spot → broker → entry)
            try:
                from app.integrations import quotes as quotes_mod
                assets = sorted({str(r.get("asset") or "").upper()
                                 for r in open_rows if r.get("asset")})
                if assets:
                    prices, _fail = await quotes_mod.fetch_spot_prices(assets)
                    for a, p in (prices or {}).items():
                        try:
                            if float(p) > 0:
                                marks["asset:" + str(a).upper()] = float(p)
                        except Exception:
                            pass
            except Exception:
                pass
            from app.services.execution import PaperBrokerPnl
            from types import SimpleNamespace
            total = 0.0
            for r in open_rows:
                try:
                    ticket = str(r.get("ticket") or "")
                    asset_u = str(r.get("asset") or "").upper()
                    mark = marks.get("asset:" + asset_u, 0) \
                        or marks.get(ticket, 0) \
                        or float(r.get("entry_price") or 0)
                    total += float(PaperBrokerPnl.compute(SimpleNamespace(
                        direction=str(r.get("direction") or "").upper(),
                        current_price=float(mark),
                        entry_price=float(r.get("entry_price") or 0),
                        volume=float(r.get("volume") or 0),
                        asset=str(r.get("asset") or ""))))
                except Exception:
                    continue
            unreal = round(total, 2)
        ctx.unrealized_pnl = unreal
        if cap > 0:
            ctx.equity = round(cap + float(ctx.pnl_total or 0) + unreal, 2)
        try:
            from app.services.execution import equity_drawdown_pct
            ctx.drawdown_pct = round(
                float(equity_drawdown_pct(db, cap) or 0), 2) if cap > 0 else 0.0
        except Exception:
            pass
    except Exception:
        pass

    return ctx


@router.post("/assess", response_model=GoalAssessment)
async def assess_goal(goal: GoalInput, request: Request) -> GoalAssessment:
    """Evaluate probability (High/Moderate/Low) + Best/Normal/Worst case scenarios.

    The result is adjusted by the user's REAL state: realized + unrealized
    PnL, win rate, top-scorer market regime, drawdown pressure, kill switch
    and pause switch. Emits a Risk Warning when the target exceeds what the
    profile can deliver or trading is blocked.
    """
    reality = await _reality_from_db(
        request.app.state.db, getattr(request.app.state, "broker", None))
    return engine.assess(goal, reality)
