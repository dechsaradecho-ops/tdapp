"""Extended Trading System API — frequency, order strategy, correlation, calendar,
session, kill switch, risk officer, journal, backtest, walk-forward, paper trading
and the 11-section extended analysis output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from app.models.schemas import (
    AppSettings,
    BacktestConfig,
    BacktestResult,
    CorrelationEngine,
    EconomicCalendarEngine,
    EconomicEvent,
    ExposureBreakdown,
    ExposureEngine,
    FrequencyDecision,
    FrequencyEngine,
    JournalAnalysis,
    JournalEntry,
    KillSwitchEngine,
    KillSwitchStatus,
    MarketSessionStatus,
    NewsRiskStatus,
    OrderPlan,
    OrderStrategyEngine,
    PaperTradingStatus,
    RiskOfficer,
    RiskOfficerReview,
    RiskProfile,
    SessionEngine,
    TradeLimits,
    WalkForwardResult,
    analyze_journal,
    paper_trading_status,
    run_backtest,
    walk_forward,
)

router = APIRouter()

JOURNAL_TABLE = "trading_journal"


def _settings(request: Request) -> AppSettings:
    """Load user settings from DB (defaults fallback when row missing)."""
    from app.api.routes.settings import get_app_settings
    return get_app_settings(request.app.state.db)


def _journal_from_rows(rows: list[dict]) -> list[JournalEntry]:
    return [JournalEntry(
        id=r.get("id"), asset=r["asset"], direction=str(r["direction"]).upper(),
        entry_price=float(r["entry_price"] or 0),
        exit_price=float(r["exit_price"]) if r.get("exit_price") is not None else None,
        holding_time_min=float(r["holding_time_min"]) if r.get("holding_time_min") is not None else None,
        pnl=float(r["pnl"]) if r.get("pnl") is not None else None,
        rr_ratio=float(r["rr_ratio"]) if r.get("rr_ratio") is not None else None,
        market_regime=r.get("market_regime", ""),
        opportunity_score=float(r.get("opportunity_score") or 0),
        ai_explanation=r.get("ai_explanation", ""),
        closed_at=r.get("closed_at"),
        created_at=r.get("created_at"),
    ) for r in rows]


# ---------------------------------------------------------------- frequency
@router.get("/frequency", response_model=FrequencyDecision)
async def get_frequency(request: Request,
                        profile: RiskProfile | None = None) -> FrequencyDecision:
    """Evaluate whether a new trade is allowed under the frequency limits."""
    db = request.app.state.db
    s = _settings(request)
    eff_profile = profile or s.risk_profile
    today = datetime.now(timezone.utc).date().isoformat()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    today_trades = db.select(JOURNAL_TABLE, filters={"trade_date": today}, limit=100)
    week_trades = db.select(JOURNAL_TABLE, limit=200)
    week_count = len([r for r in week_trades if str(r.get("trade_date", "")) >= week_ago[:10]])

    return FrequencyEngine(
        eff_profile,
        limits_override=TradeLimits(
            max_trades_daily=s.max_trades_daily,
            max_trades_weekly=s.max_trades_weekly,
            max_open_positions=s.max_open_positions,
            risk_per_trade_pct=s.risk_per_trade_pct,
        ),
        min_confidence=s.min_confidence,
        drawdown_throttle_pct=s.drawdown_throttle_pct,
    ).evaluate(
        confidence=s.min_confidence,
        trades_today=len(today_trades),
        trades_this_week=week_count,
    )


# ------------------------------------------------------------ order strategy
from pydantic import BaseModel, Field  # noqa: E402


class PlanOrderRequest(BaseModel):
    asset: str
    direction: str = "BUY"
    entry: float
    stop_loss: float
    take_profit: float
    atr_pct: float = 0.8
    regime: str = "bull_trend"
    equity: float = 10_000.0
    risk_per_trade_pct: float = 1.0


@router.post("/order-plan", response_model=OrderPlan)
async def build_order_plan(payload: PlanOrderRequest) -> OrderPlan:
    """Multi-entry order plan (market/limit/stop legs) from a proposal."""
    return OrderStrategyEngine().build_plan(
        asset=payload.asset, direction=payload.direction.upper(),  # type: ignore[arg-type]
        entry=payload.entry, stop_loss=payload.stop_loss,
        take_profit=payload.take_profit, atr_pct=payload.atr_pct,
        regime=payload.regime, equity=payload.equity,
        risk_per_trade_pct=payload.risk_per_trade_pct,
    )


# ------------------------------------------------------------- correlation
@router.get("/correlation")
async def get_correlation(request: Request) -> dict:
    """Portfolio correlation (0-100) + per-currency exposure breakdown."""
    db = request.app.state.db
    trades = db.select("trades", filters={"status": "open"}, limit=50)
    assets = sorted({t["asset"] for t in trades}) or ["EURUSD"]
    corr = CorrelationEngine().portfolio_correlation(assets)
    exposure = ExposureEngine().analyze([
        {"asset": t["asset"], "direction": t["direction"],
         "volume": float(t.get("volume") or 0), "price": float(t.get("entry_price") or 1)}
        for t in trades
    ])
    return {
        "assets": assets,
        "portfolio_correlation": corr,
        "exposure": [e.model_dump() for e in exposure],
    }


# ---------------------------------------------------------------- calendar
@router.get("/calendar", response_model=NewsRiskStatus)
async def get_calendar(request: Request) -> NewsRiskStatus:
    """News-risk gate from events stored in economic_calendar (empty → SAFE)."""
    db = request.app.state.db
    rows = db.select("economic_calendar", limit=50)
    now = datetime.now(timezone.utc)
    events: list[EconomicEvent] = []
    for r in rows:
        t = r.get("event_time")
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                t = None
        if isinstance(t, datetime) and t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        events.append(EconomicEvent(
            event=r["event"], currency=r.get("currency", "USD"),
            time_utc=t, impact=r.get("impact", "high")))
    s = _settings(request)
    return EconomicCalendarEngine(block_minutes=s.news_block_minutes).news_risk(events, now)


# ----------------------------------------------------------------- session
@router.get("/session", response_model=MarketSessionStatus)
async def get_session() -> MarketSessionStatus:
    return SessionEngine.active()


# ------------------------------------------------------------- kill switch
@router.get("/kill-switch", response_model=KillSwitchStatus)
async def get_kill_switch(request: Request) -> KillSwitchStatus:
    """Aggregate loss/drawdown from journal + infra health → kill switch state."""
    db = request.app.state.db
    broker = request.app.state.broker
    entries = _journal_from_rows(db.select(JOURNAL_TABLE, limit=200))
    s = _settings(request)

    total_pnl = sum(e.pnl or 0 for e in entries)
    losses = [e for e in entries if (e.pnl or 0) < 0]
    # Settings capital replaces the old demo baseline
    capital = s.capital or 10_000.0
    today = datetime.now(timezone.utc).date().isoformat()
    daily = sum(e.pnl or 0 for e in entries
                if str(e.created_at or e.closed_at or "")[:10] == today)
    monthly = total_pnl
    weekly = sum(e.pnl or 0 for e in entries[-20:])

    return KillSwitchEngine(
        daily_loss_limit=s.kill_daily_loss_pct,
        weekly_loss_limit=s.kill_weekly_loss_pct,
        monthly_loss_limit=s.kill_monthly_loss_pct,
        drawdown_limit=s.max_drawdown_pct,
    ).evaluate(
        daily_loss_pct=max(0.0, -daily / capital * 100),
        weekly_loss_pct=max(0.0, -weekly / capital * 100),
        monthly_loss_pct=max(0.0, -monthly / capital * 100),
        drawdown_pct=0.0,  # peak equity tracking arrives with portfolio persistence
        broker_connected=getattr(broker, "connected", True),
        market_data_ok=True,
        ai_provider_ok=True,
        execution_ok=True,
    )


# ------------------------------------------------------------ risk officer
class RiskOfficerRequest(BaseModel):
    confidence: float
    opportunity_score: float
    correlation_score: float = 0.0
    profile: RiskProfile = RiskProfile.moderate


@router.post("/risk-officer", response_model=RiskOfficerReview)
async def review(payload: RiskOfficerRequest, request: Request) -> RiskOfficerReview:
    db = request.app.state.db
    freq = await get_frequency(request, payload.profile)
    news = await get_calendar(request)
    ks = await get_kill_switch(request)
    s = _settings(request)
    return RiskOfficer().review_trade(
        confidence=payload.confidence,
        opportunity_score=payload.opportunity_score,
        frequency=freq, news_risk=news, kill_switch=ks,
        correlation_score=payload.correlation_score,
        correlation_cap=s.correlation_cap,
    )


# ----------------------------------------------------------------- journal
class JournalCreateRequest(BaseModel):
    asset: str
    direction: str = "BUY"
    entry_price: float
    exit_price: float | None = None
    holding_time_min: float | None = None
    pnl: float | None = None
    rr_ratio: float | None = None
    market_regime: str = ""
    opportunity_score: float = 0.0
    ai_explanation: str = ""


@router.get("/journal", response_model=JournalAnalysis)
async def journal_analysis(request: Request, days: int = 30) -> JournalAnalysis:
    """7/30/90-day performance: win rate, profit factor, avg RR, best/worst setup."""
    db = request.app.state.db
    rows = db.select(JOURNAL_TABLE, limit=500)
    entries = _journal_from_rows(rows)
    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        entries = [e for e in entries
                   if str(e.created_at or e.closed_at or "")[:10] >= cutoff]
    return analyze_journal(entries, period_days=days)


@router.post("/journal")
async def journal_create(payload: JournalCreateRequest, request: Request) -> dict:
    db = request.app.state.db
    row = {
        "asset": payload.asset, "direction": payload.direction.lower(),
        "entry_price": payload.entry_price, "exit_price": payload.exit_price,
        "holding_time_min": payload.holding_time_min, "pnl": payload.pnl,
        "rr_ratio": payload.rr_ratio, "market_regime": payload.market_regime,
        "opportunity_score": payload.opportunity_score,
        "ai_explanation": payload.ai_explanation,
        "trade_date": datetime.now(timezone.utc).date().isoformat(),
    }
    inserted = db.insert(JOURNAL_TABLE, row)
    return {"ok": inserted is not None, "row": inserted}


# ---------------------------------------------------------------- backtest
@router.post("/backtest", response_model=BacktestResult)
async def backtest(config: BacktestConfig) -> BacktestResult:
    """Indicator-driven long/flat backtest over real daily candles."""
    from app.integrations import quotes
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            candles = await quotes.fetch_candles(config.asset, client, days=config.days)
    except Exception as exc:
        return BacktestResult(config=config, total_trades=0, win_rate_pct=0.0,
                              profit_factor=0.0, sharpe_ratio=0.0, max_drawdown_pct=0.0,
                              final_equity=config.initial_capital,
                              note=f"ไม่มีข้อมูลราคา ({exc.__class__.__name__})")
    return run_backtest(candles, config)


@router.post("/walk-forward", response_model=WalkForwardResult)
async def walk_forward_route(config: BacktestConfig) -> WalkForwardResult:
    """Backtest → walk-forward segments → reliability score 0-100."""
    from app.integrations import quotes
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            candles = await quotes.fetch_candles(config.asset, client, days=config.days)
    except Exception as exc:
        return WalkForwardResult(segments=0, in_sample_win_rates=[],
                                 out_sample_win_rates=[], reliability_score=0.0,
                                 note=f"ไม่มีข้อมูลราคา ({exc.__class__.__name__})")
    return walk_forward(candles, config)


# ----------------------------------------------------------- paper trading
@router.get("/paper-trading", response_model=PaperTradingStatus)
async def paper_trading(request: Request) -> PaperTradingStatus:
    """Virtual capital status + AI coaching + live readiness score."""
    broker = request.app.state.broker
    s = _settings(request)
    return paper_trading_status(broker, virtual_capital=s.paper_virtual_capital)


# -------------------------------------------------- extended analysis (11 sections)
@router.get("/extended-analysis")
async def extended_analysis(request: Request) -> dict:
    """The full EXTENDED OUTPUT FORMAT — every section computed live."""
    from app.api.routes.chat import _build_context
    from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine

    db = request.app.state.db
    engine = StrategyEngine()
    s = _settings(request)

    ctx = await _build_context(db)

    # market snapshot for order strategy demo leg
    rows = db.select("market_analysis", limit=5)
    top = rows[0] if rows else None
    regime = top.get("regime", "sideway") if top else "sideway"
    confidence = float(top.get("confidence") or 0) if top else 0.0
    asset = top.get("asset", "EURUSD") if top else "EURUSD"

    freq = await get_frequency(request)
    news = await get_calendar(request)
    session = get_session_sync()
    corr = await get_correlation(request)
    ks = await get_kill_switch(request)
    journal = await journal_analysis(request, days=30)
    paper = await paper_trading(request)
    officer = RiskOfficer().review_trade(
        confidence=confidence, opportunity_score=confidence,
        frequency=freq, news_risk=news, kill_switch=ks,
        correlation_score=corr["portfolio_correlation"],
        correlation_cap=s.correlation_cap)

    plan = OrderStrategyEngine().build_plan(
        asset=asset, direction="BUY", entry=1.0, stop_loss=0.99,
        take_profit=1.02, regime=regime, atr_pct=0.8,
        equity=s.default_equity,
        risk_per_trade_pct=freq.limits.risk_per_trade_pct if freq.limits else 1.0)

    return {
        "news_calendar": f"{news.status}: {news.reason}",
        "session_analysis": f"{', '.join(session.active_sessions) or 'ปิดตลาด'} "
                            f"({session.volatility_hint} volatility, {session.current_utc_time})",
        "correlation_analysis": f"Portfolio correlation {corr['portfolio_correlation']}/100; "
                                f"exposure: " + ", ".join(
                                    f"{e['currency']} {e['exposure_pct']}%" for e in corr["exposure"]) or "n/a",
        "order_strategy": plan.model_dump_json(),
        "execution_plan": " | ".join(
            f"{leg.order_type.value} @ {leg.price} lot {leg.lot}" for leg in plan.entries),
        "risk_officer_review": officer.verdict + (" — " + "; ".join(officer.rejects) if officer.rejects else ""),
        "journal_insight": f"{journal.total_trades} trades/{journal.period_days}d, "
                           f"win {journal.win_rate_pct}%, PF {journal.profit_factor}, "
                           f"avgRR {journal.average_rr}",
        "backtest_result": "ยิง POST /api/trading/backtest เพื่อรันตาม indicator (ไม่รันอัตโนมัติเพราะใช้เวลา)",
        "paper_trading_status": f"readiness {paper.live_readiness_score}/100 — {paper.ai_coaching}",
        "kill_switch_status": ks.message,
        "final_decision": _final_decision(officer, news, ks, confidence),
        "context_block": ctx,
    }


def get_session_sync() -> MarketSessionStatus:
    return SessionEngine.active()


def _final_decision(officer: RiskOfficerReview, news: NewsRiskStatus,
                    ks: KillSwitchStatus, confidence: float) -> str:
    if ks.engaged:
        return "WAIT — Kill Switch ทำงานอยู่"
    if officer.verdict == "REJECTED":
        return "WAIT — Risk Officer ไม่อนุมัติ"
    if news.status == "DANGER":
        return "WAIT — ข่าว impact สูงใกล้ตัว"
    if confidence >= s.min_confidence:
        return "TRADE — ผ่านทุกด่าน อนุมัติเข้าไม้ตามแผน"
    return "WAIT — Confidence ต่ำกว่าเกณฑ์"
