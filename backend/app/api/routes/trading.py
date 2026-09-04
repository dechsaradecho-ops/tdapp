"""Extended Trading System API — frequency, order strategy, correlation, calendar,
session, kill switch, risk officer, journal, backtest, walk-forward, paper trading
and the 11-section extended analysis output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from app.models.close_position import ClosePositionResult
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
    MonitorSnapshot,
    NewsRiskStatus,
    OrderPlan,
    OrderStrategyEngine,
    PaperTradingStatus,
    PauseStatus,
    RiskOfficer,
    RiskOfficerReview,
    RiskProfile,
    SessionEngine,
    TradeLimits,
    WalkForwardResult,
    analyze_journal,
    effective_min_confidence,
    paper_trading_status,
    run_backtest,
    walk_forward,
)
from app.services import execution

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


# ------------------------------------------------------------ trading pause
@router.get("/pause", response_model=PauseStatus)
async def get_pause(request: Request) -> PauseStatus:
    """Live manual kill-switch state (read by UI badge + LINE /status)."""
    return execution.get_pause(request.app.state.db)


class PauseRequest(BaseModel):
    paused: bool
    reason: str = ""


@router.post("/pause", response_model=PauseStatus)
async def set_pause(payload: PauseRequest, request: Request) -> PauseStatus:
    """Engage/clear the manual kill switch — blocks BOTH auto and approved orders."""
    return execution.set_pause(request.app.state.db, payload.paused, payload.reason)


# ----------------------------------------------------------------- monitor
@router.get("/monitor", response_model=MonitorSnapshot)
async def monitor(request: Request) -> MonitorSnapshot:
    """One snapshot for the /monitor dashboard: pause state, kill switch,
    open positions with live marks + unrealized PnL, recent executions, stats."""
    db = request.app.state.db
    s = _settings(request)
    # monitor_snapshot is async: it awaits live quote marks per asset —
    # the old sync version silently dropped the broker coroutine and pinned
    # current_price to the entry price (PnL stuck at 0.00).
    return await execution.monitor_snapshot(db, request.app.state.broker, s)


# ------------------------------------------------------- manual position close
async def _spot_prices(assets: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    """Thin wrapper over quotes.fetch_spot_prices — module-level so tests can
    monkeypatch it (the endpoint imports this module, not quotes directly)."""
    from app.integrations import quotes as quotes_mod
    return await quotes_mod.fetch_spot_prices(assets)


class ClosePositionRequest(BaseModel):
    ticket: str
    close_reason: str = "manual"


@router.post("/positions/close", response_model=ClosePositionResult)
async def close_position(payload: ClosePositionRequest,
                         request: Request) -> ClosePositionResult:
    """Manually close ONE open paper position (monitor page button).

    Flow mirrors position_guard's SL/TP close: broker.close_position →
    close_trade_rows (journal) → LINE notify. The response carries a full
    summary (entry/exit/PnL/holding time + portfolio stats) so the UI can
    render the confirmation popup without a second round-trip.
    """
    import logging
    from types import SimpleNamespace

    from app.services.notification_service import NotificationService

    log = logging.getLogger(__name__)
    db = request.app.state.db
    broker = request.app.state.broker
    ticket = payload.ticket.strip()

    # ---- find the open journal row ---------------------------------------
    rows = db.select("paper_trades", filters={"ticket": ticket, "status": "open"},
                     limit=1)
    if not rows:
        return ClosePositionResult(
            ok=False, ticket=ticket,
            message=f"ไม่พบไม้ที่เปิดอยู่กับ ticket {ticket} "
                    f"(อาจถูกปิดไปแล้วโดย SL/TP)")
    row = rows[0]

    # ---- resolve the exit mark (live feed → broker book → entry) ---------
    # Same priority as monitor_snapshot.mark_for: the live spot feed is the
    # real market; the broker book is only a fallback for uncovered assets.
    exit_price = 0.0
    try:
        asset = str(row.get("asset") or "").upper()
        prices, _failures = await _spot_prices([asset])
        exit_price = float(prices.get(asset) or 0)
    except Exception as exc:
        log.warning("close %s: live mark unavailable: %s", ticket, exc)
    if not exit_price:
        try:
            exit_price = float(await broker.mark_price(ticket))
        except Exception:
            exit_price = 0.0
    if not exit_price:
        try:
            exit_price = float(await broker.quote(row.get("asset", "")))
        except Exception:
            exit_price = 0.0
    if not exit_price:
        exit_price = float(row.get("entry_price") or 0)  # last resort: flat PnL

    # ---- close at the broker ---------------------------------------------
    result = await broker.close_position(ticket)
    if not result.ok:
        return ClosePositionResult(
            ok=False, ticket=ticket, asset=str(row.get("asset") or ""),
            message=f"ปิดไม่สำเร็จ: {result.message}")

    # ---- compute PnL (same math as the monitor + guard) -------------------
    pos = SimpleNamespace(
        direction=str(row.get("direction") or "BUY").upper(),
        current_price=exit_price,
        entry_price=float(row.get("entry_price") or 0),
        volume=float(row.get("volume") or 0),
        asset=str(row.get("asset") or ""),
    )
    pnl = round(execution.PaperBrokerPnl.compute(pos), 2)
    entry = float(row.get("entry_price") or 0)
    capital = max(_settings(request).capital, 1.0)
    pnl_pct = round(pnl / capital * 100, 2)

    # ---- holding time -----------------------------------------------------
    holding_min: float | None = None
    created_raw = row.get("created_at")
    if created_raw:
        try:
            created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            holding_min = round((datetime.now(timezone.utc) - created).total_seconds() / 60, 1)
        except ValueError:
            holding_min = None

    # ---- journal + notify -------------------------------------------------
    execution.close_trade_rows(db, ticket, exit_price, pnl, payload.close_reason)
    warnings: list[str] = []
    try:
        notifier = NotificationService(db, request.app.state.line)
        emoji = "✋"
        await notifier.notify(
            row.get("user_id", ""), "trade_closed",
            f"{emoji} Manual Close\n"
            f"Asset: {row.get('asset')}\nDirection: {pos.direction}\n"
            f"Entry: {entry:g} → Exit: {exit_price:g}\n"
            f"PnL: {pnl:+,.2f} ({pnl_pct:+.2f}%)",
        )
    except Exception as exc:
        warnings.append(f"notify failed: {exc}")

    # ---- portfolio summary for the popup ----------------------------------
    all_rows = db.select("paper_trades", limit=500)
    closed = [r for r in all_rows
              if r.get("status") == "closed" and r.get("pnl") is not None]
    open_count = len([r for r in all_rows if r.get("status") == "open"])
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    pnl_today = round(sum(float(r.get("pnl") or 0) for r in closed
                          if str(r.get("closed_at") or r.get("created_at") or "")[:10] == today), 2)
    wins = len([r for r in closed if float(r.get("pnl") or 0) > 0])
    losses = len([r for r in closed if float(r.get("pnl") or 0) < 0])

    # journal row id of the just-closed trade (close_trade_rows updated it)
    trade_id = ""
    try:
        updated = db.select("paper_trades", filters={"ticket": ticket}, limit=1)
        if updated:
            trade_id = str(updated[0].get("id") or "")
    except Exception:
        trade_id = ""

    return ClosePositionResult(
        ok=True, ticket=ticket, asset=str(row.get("asset") or ""),
        direction=pos.direction, volume=pos.volume,
        entry_price=entry, exit_price=exit_price, pnl=pnl, pnl_pct=pnl_pct,
        holding_time_min=holding_min, close_reason=payload.close_reason,
        message=result.message,
        remaining_open=open_count,
        total_realized_pnl=round(sum(float(r.get("pnl") or 0) for r in closed), 2),
        pnl_today=pnl_today, wins=wins, losses=losses,
        trade_id=trade_id, warnings=warnings,
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
        "final_decision": _final_decision(officer, news, ks, confidence, asset, s),
        "context_block": ctx,
    }


def get_session_sync() -> MarketSessionStatus:
    return SessionEngine.active()


def _final_decision(officer: RiskOfficerReview, news: NewsRiskStatus,
                    ks: KillSwitchStatus, confidence: float,
                    asset: str = "", s: AppSettings | None = None) -> str:
    if ks.engaged:
        return "WAIT — Kill Switch ทำงานอยู่"
    if officer.verdict == "REJECTED":
        return "WAIT — Risk Officer ไม่อนุมัติ"
    if news.status == "DANGER":
        return "WAIT — ข่าว impact สูงใกล้ตัว"
    # Per-asset quality gate — gold uses Min Confidence (gold) when set.
    threshold = (effective_min_confidence(s, asset)
                 if s is not None else 70.0)
    if confidence >= threshold:
        return "TRADE — ผ่านทุกด่าน อนุมัติเข้าไม้ตามแผน"
    return "WAIT — Confidence ต่ำกว่าเกณฑ์"
