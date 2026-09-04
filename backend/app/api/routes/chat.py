"""AI Chat Assistant endpoint — answers grounded in engine outputs."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.integrations.ai_provider import build_context_block, get_ai_provider
from app.models.schemas import ChatRequest, ChatResponse
from app.engine.goal_engine import GoalEngine
from app.engine.risk_engine import PortfolioSnapshot, RiskEngine
from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine, regime_of
from app.integrations import quotes
from app.api.routes.settings import get_app_settings
from app.models.schemas import GoalInput
from app.api.routes.market import DEMO as market_demo
from app.services.execution import monitor_snapshot

router = APIRouter()


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    """Every reply is grounded in real Market Condition / Risk / Orders / Portfolio status."""
    db = request.app.state.db
    context = await _build_context(db, request.app.state.broker)

    provider = get_ai_provider()
    messages = [m.model_dump() for m in payload.messages]
    messages[-1] = {"role": "user", "content": f"{messages[-1]['content']}\n\n{context}"}

    reply = await provider.chat(messages)
    return ChatResponse(
        reply=reply,
        grounded_on=["market_condition", "risk_analysis", "opportunity_score",
                     "portfolio_status", "open_orders", "recent_orders"],
    )


@router.post("/stream")
async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    """Same grounded reply, but streamed progressively.

    The UI renders chunks as they arrive so the user sees the AI "thinking"
    instead of staring at a frozen loader for the whole generation time.
    Protocol: plain-text chunks concatenated in order (frontend uses a
    ReadableStream reader + TextDecoder).
    """
    db = request.app.state.db

    async def gen():
        try:
            messages = [m.model_dump() for m in payload.messages]
            # context is built once here (DB + engines are fast); tokens stream after
            ctx = await _build_context(db, request.app.state.broker)
            messages[-1] = {"role": "user", "content": f"{messages[-1]['content']}\n\n{ctx}"}
            provider = get_ai_provider()
            async for piece in provider.chat_stream(messages):
                yield piece
        except Exception as exc:  # never kill the stream mid-flight
            yield f"[AI ERROR] {exc.__class__.__name__}: {exc}"

    return StreamingResponse(
        gen(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _build_context(db, broker=None) -> str:
    """Shared grounded-context block for both /api/chat and /api/chat/stream."""
    engine = StrategyEngine()

    # ---- Live market context: worker rows → live quotes → demo constants ----
    rows = db.select("market_analysis", limit=25)
    seen: set[str] = set()
    per_asset: dict[str, tuple[float, str]] = {}
    regime_str, regime_confidence = "sideway", 50.0
    for row in rows:
        if row["asset"] in seen:
            continue
        seen.add(row["asset"])
        per_asset[row["asset"]] = (float(row["confidence"]), row.get("regime", ""))
    if per_asset:
        top_asset, (top_score, top_regime) = max(per_asset.items(), key=lambda kv: kv[1][0])
        regime_str = top_regime or "sideway"
        regime_confidence = top_score

    if not per_asset:
        try:
            snaps = await quotes.fetch_all_snapshots(
                ["XAUUSD", "EURUSD", "USDJPY", "GBPUSD", "AUDUSD"])
            for asset, snap in snaps.items():
                ind = IndicatorSnapshot(**{**snap, "source": "live"})
                opp = engine.opportunity_score(ind)
                per_asset[asset] = (opp.score, regime_of(ind))
        except Exception:
            pass
        if per_asset:
            top_asset, (top_score, top_regime) = max(per_asset.items(), key=lambda kv: kv[1][0])
            regime_str, regime_confidence = top_regime, top_score
    if not per_asset:
        per_asset = {a: (engine.opportunity_score(market_demo[a]).score, "sideway")
                     for a in market_demo}
        top_asset, (top_score, top_regime) = max(per_asset.items(), key=lambda kv: kv[1][0])
        regime_str, regime_confidence = top_regime, top_score

    opportunity_str = " / ".join(
        f"{a} {score:.0f}" for a, (score, _) in
        sorted(per_asset.items(), key=lambda kv: -kv[1][0]))

    # ---- Goal/risk context — capital/profile from saved settings (single source) ----
    s = get_app_settings(db)
    cap = s.capital or 100_000.0
    goal = GoalEngine().assess(GoalInput(
        capital=cap, target_return_pct=3.0,
        max_drawdown_pct=s.max_drawdown_pct, risk_profile=s.risk_profile,
    ))

    # ---- REAL portfolio + orders (monitor snapshot — same data the dashboard shows) ----
    # Fail-safe: a broken broker/DB degrades to zeros, never raises.
    snap = None
    try:
        snap = await monitor_snapshot(db, broker, s)
    except Exception:
        snap = None
    st = snap.stats if snap else None
    open_pos = snap.open_positions if snap else []
    recent = snap.recent if snap else []

    def _fmt_pos(p) -> str:
        return (f"{p.asset} {p.direction} {p.volume} lot @ {p.entry_price:.5g} "
                f"mark {p.current_price:.5g} uPnL {p.unrealized_pnl:+.2f} "
                f"SL {p.stop_loss if p.stop_loss is not None else '-'} "
                f"TP {p.take_profit if p.take_profit is not None else '-'} "
                f"[{p.source}]")

    def _fmt_trade(t) -> str:
        pnl = f"{t.pnl:+.2f}" if t.pnl is not None else "-"
        return (f"{t.asset} {t.direction} {t.volume} lot entry {t.entry_price:.5g} "
                f"exit {t.exit_price if t.exit_price is not None else '-'} "
                f"pnl {pnl} [{t.status}/{t.close_reason or '-'}]")

    open_orders_str = ("; ".join(_fmt_pos(p) for p in open_pos)
                       if open_pos else "none (no open positions)")
    recent_orders_str = ("; ".join(_fmt_trade(t) for t in recent[:10])
                         if recent else "none (no orders yet)")

    # Realized PnL from closed trades (real, not the old hardcoded demo values)
    pnl_today = st.pnl_today if st else 0.0
    pnl_week = st.pnl_week if st else 0.0
    pnl_total = st.pnl_total if st else 0.0
    unrealized_total = round(sum(p.unrealized_pnl for p in open_pos), 2)
    equity = round(cap + pnl_total + unrealized_total, 2)

    risk = RiskEngine().check(PortfolioSnapshot(
        starting_capital=cap, peak_equity=max(cap, equity), current_equity=equity,
        realized_pnl_today=pnl_today, realized_pnl_week=pnl_week,
        realized_pnl_month=pnl_total, open_risk=cap * 0.005,
    ))

    portfolio_status = (
        f"capital={cap:.2f}, realized_pnl_total={pnl_total:+.2f}, "
        f"unrealized_pnl_open={unrealized_total:+.2f}, equity={equity:.2f}, "
        f"pnl_today={pnl_today:+.2f}, pnl_week={pnl_week:+.2f}, "
        f"win_rate={st.win_rate if st else 0.0}%, closed_trades={st.closed_count if st else 0}, "
        f"open_positions={len(open_pos)}, "
        f"goal_probability={goal.probability.value}, "
        f"normal_case={goal.scenarios[1].expected_return_pct}%"
    )

    return build_context_block({
        "market_condition": f"{regime_str} (score {regime_confidence:.0f}%), "
                            f"top asset {top_asset}",
        "opportunity_score": opportunity_str,
        "risk_analysis": risk.model_dump(),
        "portfolio_status": portfolio_status,
        "open_orders": open_orders_str,
        "recent_orders": recent_orders_str,
    })
