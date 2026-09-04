"""Goal Engine endpoint — assess feasibility of a monthly return target.

Reality-aware (2026-09-04): the assessment folds in the user's LIVE trading
state — realized PnL + win rate from closed paper_trades, the current market
regime from market_analysis (worker rows), the kill switch state and the
manual pause switch. Every read is fail-safe: a broken DB or empty tables
degrade to the pure envelope math, never raise.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.engine.goal_engine import GoalEngine
from app.models.schemas import GoalAssessment, GoalInput, GoalRealityContext

router = APIRouter()
engine = GoalEngine()


def _reality_from_db(db) -> GoalRealityContext:
    """Collect live portfolio + market state (all reads fail-safe → defaults)."""
    ctx = GoalRealityContext()

    if not db or not getattr(db, "available", False):
        return ctx

    # ---- 1) Portfolio stats from closed paper_trades (same math as monitor) --
    try:
        rows = db.select("paper_trades", limit=500)
        closed = [r for r in rows
                  if r.get("status") == "closed" and r.get("pnl") is not None]
        wins = [r for r in closed if float(r.get("pnl") or 0) > 0]
        ctx.closed_count = len(closed)
        ctx.pnl_total = round(sum(float(r.get("pnl") or 0) for r in closed), 2)
        ctx.win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
        ctx.open_positions = len([r for r in rows if r.get("status") == "open"])
        ctx.data_available = bool(closed) or bool(ctx.open_positions)
    except Exception:
        pass  # stats unavailable → envelope math only

    # ---- 2) Market regime from market_analysis (worker rows, newest first) ---
    try:
        analysis = db.select("market_analysis", limit=25)
        if analysis:
            top = analysis[0]  # select() orders by created_at desc
            ctx.market_regime = str(top.get("regime") or "sideway")
            ctx.market_sentiment = str(top.get("sentiment") or "neutral")
            ctx.data_available = True
    except Exception:
        pass

    # ---- 3) Kill switch (same loss math the gate uses) -----------------------
    try:
        from app.services.execution import _loss_pcts
        from app.api.routes.settings import get_app_settings
        from app.models.schemas import KillSwitchEngine

        s = get_app_settings(db)
        daily, weekly, monthly = _loss_pcts(db, s.capital)
        ks = KillSwitchEngine(
            daily_loss_limit=s.kill_daily_loss_pct,
            weekly_loss_limit=s.kill_weekly_loss_pct,
            monthly_loss_limit=s.kill_monthly_loss_pct,
            drawdown_limit=s.max_drawdown_pct,
        ).evaluate(
            daily_loss_pct=daily, weekly_loss_pct=weekly, monthly_loss_pct=monthly,
            drawdown_pct=0.0,
            broker_connected=True, market_data_ok=True,
            ai_provider_ok=True, execution_ok=True,
        )
        ctx.kill_switch_engaged = ks.engaged
        ctx.kill_triggers = list(ks.triggers)
    except Exception:
        pass  # kill state unknown → treat as clear (fail-open for assessment only)

    # ---- 4) Manual pause switch ----------------------------------------------
    try:
        from app.services.execution import get_pause
        pause = get_pause(db)
        ctx.trading_paused = pause.paused
        ctx.pause_reason = pause.reason or ""
    except Exception:
        pass

    return ctx


@router.post("/assess", response_model=GoalAssessment)
async def assess_goal(goal: GoalInput, request: Request) -> GoalAssessment:
    """Evaluate probability (High/Moderate/Low) + Best/Normal/Worst case scenarios.

    The result is adjusted by the user's REAL state: realized PnL, win rate,
    current market regime, kill switch and pause switch. Emits a Risk Warning
    when the target exceeds what the profile can deliver or trading is blocked.
    """
    reality = _reality_from_db(request.app.state.db)
    return engine.assess(goal, reality)
