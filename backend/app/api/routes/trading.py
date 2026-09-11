"""Extended Trading System API — frequency, order strategy, correlation, calendar,
session, kill switch, risk officer, journal, backtest, walk-forward, paper trading
and the 11-section extended analysis output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from app.models.close_position import ClosePositionResult
from app.models.stats_reset import StatsResetResult
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
from app.services import signal_log
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
    ) for r in rows if r.get("asset")]


def _journal_entries_from_paper_trades(rows: list[dict]) -> list[JournalEntry]:
    """Map CLOSED paper_trades rows (the live journal) → JournalEntry.

    paper_trades has no rr_ratio/holding_time columns — both are derived:
    RR = signed exit move ÷ entry→SL distance, holding = closed_at −
    created_at. Rows without an exit pnl are skipped (open/rejected).
    """
    out: list[JournalEntry] = []
    for r in rows:
        if r.get("status") != "closed" or r.get("pnl") is None:
            continue
        try:
            direction = str(r.get("direction") or "BUY").upper()
            entry = float(r.get("entry_price") or 0)
            exit_px = float(r.get("exit_price")) if r.get("exit_price") is not None else None
            sl = float(r["stop_loss"]) if r.get("stop_loss") is not None else None
            rr: float | None = None
            if entry and sl and abs(entry - sl) > 1e-9 and exit_px is not None:
                sign = 1.0 if direction == "BUY" else -1.0
                rr = round(sign * (exit_px - entry) / abs(entry - sl), 2)
            holding: float | None = None
            try:
                from datetime import datetime as _dt
                c_raw, x_raw = r.get("created_at"), r.get("closed_at")
                if c_raw and x_raw:
                    c = _dt.fromisoformat(str(c_raw).replace("Z", "+00:00"))
                    x = _dt.fromisoformat(str(x_raw).replace("Z", "+00:00"))
                    if c.tzinfo is None:
                        from datetime import timezone as _tz
                        c = c.replace(tzinfo=_tz.utc)
                    if x.tzinfo is None:
                        from datetime import timezone as _tz
                        x = x.replace(tzinfo=_tz.utc)
                    holding = round((x - c).total_seconds() / 60, 1)
            except (ValueError, TypeError):
                holding = None
            out.append(JournalEntry(
                id=r.get("id"), asset=str(r.get("asset") or ""),
                direction=direction,  # type: ignore[arg-type]
                entry_price=entry, exit_price=exit_px,
                holding_time_min=holding,
                pnl=float(r.get("pnl") or 0), rr_ratio=rr,
                market_regime="", opportunity_score=0.0,
                ai_explanation=str(r.get("close_reason") or ""),
                closed_at=r.get("closed_at"), created_at=r.get("created_at"),
            ))
        except (ValueError, TypeError, KeyError):
            continue
    return out


# ---------------------------------------------------------------- frequency
def _frequency_counts(db) -> tuple[int, int, int]:
    """Shared today/week/open counting over paper_trades (the live journal).

    Single source for the /frequency badge, Extended and the execution
    gate — all three must see the same quotas or the UI drifts from the
    real gate (2026-09-11: Extended FINAL always WAIT while /approve opened).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        all_trades = db.select("paper_trades", limit=500)
    except Exception:
        all_trades = []
    today_count = len([r for r in all_trades
                       if str(r.get("created_at", ""))[:10] == today
                       and r.get("status") != "rejected"])
    week_count = len([r for r in all_trades
                      if str(r.get("created_at", "")) >= week_ago[:10]
                      and r.get("status") != "rejected"])
    try:
        open_rows = db.select("paper_trades", filters={"status": "open"},
                              limit=100)
        open_count = len(open_rows or [])
    except Exception:
        open_count = 0
    return today_count, week_count, open_count


def _evaluate_frequency(db, s: AppSettings, confidence: float,
                        asset: str = "",
                        profile: RiskProfile | None = None) -> FrequencyDecision:
    """Frequency check aligned with the execution gate (execution.py Gate 2).

    Regime gating belongs to the scanner, not the executor — the gate
    evaluates with regime="bull_trend" so a trend top-scorer is not
    throttled by the sideway default. Quality bar uses the per-asset
    threshold (gold override) so Extended and /approve never drift apart.
    """
    today_count, week_count, open_count = _frequency_counts(db)
    return FrequencyEngine(
        profile or s.risk_profile,
        limits_override=TradeLimits(
            max_trades_daily=s.max_trades_daily,
            max_trades_weekly=s.max_trades_weekly,
            max_open_positions=s.max_open_positions,
            risk_per_trade_pct=s.risk_per_trade_pct,
        ),
        min_confidence=effective_min_confidence(s, asset),
        drawdown_throttle_pct=s.drawdown_throttle_pct,
    ).evaluate(
        confidence=confidence,
        trades_today=today_count,
        trades_this_week=week_count,
        open_positions=open_count,
        regime="bull_trend",  # regime gating belongs to the scanner
        volatility_index=0.0,
    )


@router.get("/frequency", response_model=FrequencyDecision)
async def get_frequency(request: Request,
                        profile: RiskProfile | None = None) -> FrequencyDecision:
    """Evaluate whether a new trade is allowed under the frequency limits.

    Counts paper_trades (the live journal) — the old version read the legacy
    manual trading_journal table, so it always reported 0 while real orders
    fired through paper_trades. Same counting as the execution gate.
    """
    db = request.app.state.db
    s = _settings(request)
    return _evaluate_frequency(db, s, confidence=float(s.min_confidence),
                               asset="", profile=profile)


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


class ExtendedOpenRequest(BaseModel):
    confirm: bool = False


@router.post("/extended-open")
async def extended_open(payload: ExtendedOpenRequest,
                        request: Request) -> dict:
    """Open ONLY the first market leg of the Extended ORDER STRATEGY plan.

    Safety contract (user 2026-09-11):
      - FINAL DECISION must be TRADE — WAIT blocks, never bypassed.
      - Only entries[0] opens, and only when it is a market leg. Trend
        plans start with market + 2 limits; sideway plans start with stops
        (no market leg) → blocked with an honest reason.
      - Same single execution path as /approve + auto-trader
        (execute_signal: live re-anchor → gates → sizing → journal +
        signal_logs + LINE notify). No custom duplicate notify/log here.
    """
    import json as _json

    from app.services.notification_service import NotificationService

    db = request.app.state.db
    if not payload.confirm:
        return {"ok": False, "status": "need_confirm",
                "message": "ต้องยืนยัน (confirm=true) ก่อนเปิดออเดอร์"}

    # Reuse the live Extended computation (top scorer + real proposal +
    # officer + final decision) so the button can never drift from the box.
    body = await extended_analysis(request)
    final = str(body.get("final_decision") or "")
    if final.startswith("WAIT"):
        signal_log.log_event(
            db=db, event="order_blocked", asset="", direction="",
            source="extended", reason=f"FINAL DECISION เป็น WAIT — {final}")
        return {"ok": False, "status": "blocked", "final_decision": final,
                "rejects": [final or "FINAL DECISION เป็น WAIT"],
                "message": f"ไม่เปิดออเดอร์ — {final}"}

    try:
        plan = _json.loads(str(body.get("order_strategy") or "{}"))
    except (ValueError, TypeError):
        plan = {}
    legs = list((plan or {}).get("entries") or [])
    if not legs:
        return {"ok": False, "status": "blocked", "final_decision": final,
                "rejects": ["แผนไม่มีขาให้เปิด"],
                "message": "ไม่เปิดออเดอร์ — แผนไม่มีขาให้เปิด"}
    first = dict(legs[0] or {})
    if str(first.get("order_type") or "").lower() != "market":
        reason = (f"ขาแรกเป็น {first.get('order_type')} ไม่ใช่ market — "
                  "แผน breakout (sideway) ให้รอ trigger ไม่ยิง market แทน")
        signal_log.log_event(
            db=db, event="order_blocked",
            asset=str((plan or {}).get("asset") or ""),
            direction=str((plan or {}).get("direction") or ""),
            source="extended", reason=reason)
        return {"ok": False, "status": "blocked", "final_decision": final,
                "rejects": [reason],
                "message": f"ไม่เปิดออเดอร์ — {reason}"}

    asset = str((plan or {}).get("asset") or "").upper()
    direction = str((plan or {}).get("direction") or "BUY").upper()
    try:
        entry = float(first.get("price") or (plan or {}).get("average_entry") or 0)
    except (TypeError, ValueError):
        entry = 0.0
    try:
        stop_loss = float((plan or {}).get("stop_loss") or 0) or None
    except (TypeError, ValueError):
        stop_loss = None
    try:
        take_profit = float((plan or {}).get("take_profit") or 0) or None
    except (TypeError, ValueError):
        take_profit = None
    # The lot of the leg the user REVIEWED and confirmed. Forwarded to
    # execute_signal so the placed volume can never exceed the plan (prod
    # 2026-09-11: the plan showed lot 0.01 for the market leg but the order
    # opened 0.04 — execute_signal had re-sized for the FULL risk budget
    # instead of leg#1's 50% share). 0/absent → normal risk sizing.
    try:
        plan_volume = float(first.get("lot") or 0)
    except (TypeError, ValueError):
        plan_volume = 0.0

    # Confidence for the gate = the SAME proposal confidence FINAL used
    # (extended_analysis recomputed it from the live snapshot; the scanner
    # row can be stale/lower and would drift the gate away from FINAL).
    try:
        confidence = float(body.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not confidence:
        try:
            _rows = db.select("market_analysis", limit=50) or []
        except Exception:
            _rows = []
        _seen: set[str] = set()
        try:
            for _r in _rows:
                _a = str(_r.get("asset") or "").upper()
                if not _a or _a in _seen:
                    continue
                _seen.add(_a)
                if _a == asset:
                    confidence = float(_r.get("confidence") or 0)
                    break
        except (TypeError, ValueError):
            pass

    s = _settings(request)
    broker = request.app.state.broker
    notifier = NotificationService(db, request.app.state.line)
    report = await execution.execute_signal(
        db, broker, notifier, s,
        user_id=execution.DEFAULT_USER,
        asset=asset, direction=direction,  # type: ignore[arg-type]
        entry=entry, stop_loss=stop_loss, take_profit=take_profit,
        confidence=confidence, opportunity=confidence,
        signal_id=None, source="extended", volume=plan_volume,
    )
    if not report.allowed:
        return {"ok": False, "status": "blocked", "final_decision": final,
                "rejects": report.rejects, "checks": report.checks,
                "asset": asset, "direction": direction,
                "message": "ไม่เปิดออเดอร์ — " + ("; ".join(report.rejects[:2])
                                                  or "gate blocked")}

    # Newest open row for this asset = the ticket just created (select is
    # newest-first both in prod and FakeDatabase).
    ticket = ""
    try:
        _open = db.select("paper_trades", filters={"status": "open"},
                          limit=10) or []
        for _r in _open:
            if str(_r.get("asset") or "").upper() == asset:
                ticket = str(_r.get("ticket") or "")
                break
        if not ticket and _open:
            ticket = str(_open[0].get("ticket") or "")
    except Exception:
        ticket = ""
    return {"ok": True, "status": "executed", "final_decision": final,
            "asset": asset, "direction": direction, "ticket": ticket,
            "volume": report.size_lots, "checks": report.checks,
            "warnings": report.warnings,
            "remaining_legs": len(legs) - 1,
            "message": (f"เปิดขา Market แล้ว {direction} {asset} "
                        f"{report.size_lots:g} lots"
                        + (f" (ticket {ticket})" if ticket else ""))}


# ------------------------------------------------------------- correlation
@router.get("/correlation")
async def get_correlation(request: Request) -> dict:
    """Portfolio correlation (0-100) + per-currency exposure breakdown.

    Reads OPEN paper_trades (the live journal) — the old version read the
    legacy `trades` table (migration 001, never written by the live path),
    so it always reported a dead EURUSD/0/[] card while real positions were
    open. Fail-safe: a broken read degrades to empty, never raises.
    """
    db = request.app.state.db
    try:
        trades = db.select("paper_trades", filters={"status": "open"}, limit=100)
    except Exception:
        trades = []
    assets = sorted({str(t.get("asset") or "") for t in trades if t.get("asset")})
    corr = CorrelationEngine().portfolio_correlation(assets)
    exposure = ExposureEngine().analyze([
        {"asset": t["asset"], "direction": t.get("direction", ""),
         "volume": float(t.get("volume") or 0), "price": float(t.get("entry_price") or 1)}
        for t in trades if t.get("asset")
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
    """Kill switch state — SINGLE shared path (see execution.evaluate_kill).

    Loss/drawdown math comes from paper_trades + equity_snapshots (the same
    source the entry gate, guard emergency exit and monitor banner use), so
    the endpoint can never drift apart. The old inline version read the
    legacy trading_journal table with its own daily/weekly/monthly math.
    """
    db = request.app.state.db
    broker = request.app.state.broker
    s = _settings(request)
    return execution.evaluate_kill(
        db, s,
        broker_connected=getattr(broker, "connected", True),
    )


# ------------------------------------------------------------ risk officer
class RiskOfficerRequest(BaseModel):
    confidence: float
    opportunity_score: float
    correlation_score: float = 0.0
    profile: RiskProfile = RiskProfile.moderate
    asset: str = ""  # picks the gold Min Confidence override when set


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
        min_confidence=effective_min_confidence(s, payload.asset),
        min_opportunity=s.min_opportunity,
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
    # Lifecycle log: manual close (user clicked ปิดไม้) — reason records why.
    signal_log.log_event(
        db=db, event="closed", asset=str(row.get("asset") or ""),
        direction=str(row.get("direction") or ""), entry=entry,
        exit_price=exit_price, pnl=pnl, ticket=str(ticket),
        source="user",
        reason=f"ปิดไม้เอง ({payload.close_reason}) @ {exit_price:g}")
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


# ------------------------------------------------- manual SL/TP level adjust
class AdjustLevelsRequest(BaseModel):
    """Manual SL/TP adjust (monitor page). At least one level required."""
    ticket: str
    stop_loss: float | None = None
    take_profit: float | None = None


@router.post("/positions/levels")
async def adjust_levels(payload: AdjustLevelsRequest, request: Request) -> dict:
    """Manually move SL and/or TP of ONE open position (monitor page).

    Mirrors the guard's SL move path: broker modify → persist_sl_move /
    persist_tp_move (journal + move metadata, migration 021) → lifecycle log
    → LINE notify. The monitor page badges cells whose value differs from
    the initial levels and tooltips the move details.
    """
    import logging
    from app.services.notification_service import NotificationService

    log = logging.getLogger(__name__)
    db = request.app.state.db
    broker = request.app.state.broker
    ticket = payload.ticket.strip()

    if payload.stop_loss is None and payload.take_profit is None:
        return {"ok": False, "message": "ระบุ SL หรือ TP ใหม่อย่างน้อยหนึ่งค่า"}

    rows = db.select("paper_trades", filters={"ticket": ticket, "status": "open"},
                     limit=1)
    if not rows:
        return {"ok": False,
                "message": f"ไม่พบไม้ที่เปิดอยู่กับ ticket {ticket}"}
    row = rows[0]
    asset = str(row.get("asset") or "")

    moved: list[str] = []
    errors: list[str] = []

    if payload.stop_loss is not None:
        result = await broker.modify_stop_loss(ticket, float(payload.stop_loss))
        if result.ok:
            execution.persist_sl_move(db, ticket, float(payload.stop_loss),
                                      "manual (monitor)")
            moved.append(f"SL {payload.stop_loss:g}")
        else:
            errors.append(f"SL: {result.message}")

    if payload.take_profit is not None:
        result = await broker.modify_take_profit(ticket, float(payload.take_profit))
        if result.ok:
            execution.persist_tp_move(db, ticket, float(payload.take_profit),
                                      "manual (monitor)")
            moved.append(f"TP {payload.take_profit:g}")
        else:
            errors.append(f"TP: {result.message}")

    if not moved:
        return {"ok": False, "message": "; ".join(errors) or "ปรับระดับไม่สำเร็จ"}

    reason = f"ปรับด้วยมือจากหน้า monitor → {' · '.join(moved)}"
    signal_log.log_event(
        db=db, event="order_opened", asset=asset,
        direction=str(row.get("direction") or ""),
        entry=float(row.get("entry_price") or 0),
        stop_loss=float(payload.stop_loss) if payload.stop_loss is not None else None,
        take_profit=float(payload.take_profit) if payload.take_profit is not None else None,
        ticket=ticket, source="auto", reason=reason)

    try:
        notifier = NotificationService(db, request.app.state.line)
        moved_lines = "\n".join(moved)
        await notifier.notify(
            str(row.get("user_id") or "demo"), "trade_opened",
            f"🔧 Levels Adjusted\nAsset: {asset}\n"
            f"{moved_lines}\nTicket: {ticket}")
    except Exception as exc:
        log.debug("levels-adjust notify failed: %s", exc)

    message = f"ปรับ{' และ '.join(moved)} เรียบร้อย"
    if errors:
        message += f" (บางส่วนไม่สำเร็จ: {'; '.join(errors)})"
    return {"ok": True, "message": message, "moved": moved, "errors": errors}


# ----------------------------------------------------------------- stats reset
class StatsResetRequest(BaseModel):
    confirm: bool = False


def _reset_equity_history(db, request: Request) -> None:
    """Wipe equity_snapshots and reseed one row at the starting capital.

    The home page's Current Equity / Current PnL are computed from
    paper_trades + this history, so a stats reset must clear it too or the
    old drawdown curve keeps the kill switch throttled after a reset.
    Fail-safe: any DB error is swallowed (the reset itself still succeeds).
    """
    try:
        for r in db.select("equity_snapshots", limit=500):
            db.delete("equity_snapshots", {"id": r.get("id")})
    except Exception:
        pass
    try:
        capital = _settings(request).capital
        db.insert("equity_snapshots", {
            "user_id": execution.DEFAULT_USER,
            "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
            "equity": round(capital, 2),
        })
    except Exception:
        pass


@router.post("/stats/reset", response_model=StatsResetResult)
async def reset_stats(payload: StatsResetRequest,
                      request: Request) -> StatsResetResult:
    """Reset the monitor statistics (🗑 รีเซ็ตสถิติ button).

    Every monitor stat (PnL วันนี้ / PnL 7 วัน / PnL รวม, Win Rate,
    ไม้ที่ปิดแล้ว) is derived from CLOSED paper_trades rows, so the reset
    deletes exactly those. OPEN positions are preserved — the SL/TP guard
    and the monitor still need them. Side effect (intended): the kill
    switch's realized-loss counters also start fresh.

    Requires confirm=true (the UI sends it after window.confirm) so a stray
    POST can never wipe history.
    """
    import logging

    from app.services.notification_service import NotificationService

    log = logging.getLogger(__name__)
    db = request.app.state.db

    if not payload.confirm:
        return StatsResetResult(
            ok=False, deleted=0,
            message="ต้องยืนยัน (confirm=true) ก่อนรีเซ็ตสถิติ")

    # Equity history เป็นส่วนหนึ่งของสถิติ — เคลียร์ทุก path แล้วเขียน snapshot
    # ใหม่ที่ทุนเริ่มต้น เพื่อให้ Current Equity/PnL (หน้าแรก) กลับจุดเริ่มต้นจริง
    _reset_equity_history(db, request)

    closed_rows = db.select_paged("paper_trades",
                                  filters={"status": "closed"})
    if not closed_rows:
        return StatsResetResult(
            ok=True, deleted=0,
            message="ไม่มีสถิติให้รีเซ็ต (ไม่มีไม้ที่ปิดแล้ว)",
            stats=_fresh_stats(db))

    deleted = 0
    for r in closed_rows:
        if db.delete("paper_trades", {"id": r.get("id"), "status": "closed"}):
            deleted += 1

    # Lifecycle log: one row per reset (source=user) — the audit trail shows
    # WHEN stats were wiped, since the closed trades themselves are gone.
    signal_log.log_event(
        db=db, event="closed", source="user",
        reason=f"รีเซ็ตสถิติ — ลบไม้ที่ปิดแล้ว {deleted} ไม้")

    warnings: list[str] = []
    try:
        notifier = NotificationService(db, request.app.state.line)
        await notifier.notify(
            "", "trade_closed",
            f"🗑 Stats Reset\nลบสถิติการเทรดที่ปิดแล้ว {deleted} ไม้ "
            f"(ไม้ที่เปิดค้างยังอยู่)",
        )
    except Exception as exc:
        warnings.append(f"notify failed: {exc}")

    log.info("stats reset: deleted %s closed paper_trades", deleted)
    return StatsResetResult(
        ok=True, deleted=deleted,
        message=f"รีเซ็ตสถิติแล้ว — ลบไม้ที่ปิดแล้ว {deleted} ไม้ "
                f"+ เคลียร์ equity history (ไม้ที่เปิดค้างยังอยู่)",
        stats=_fresh_stats(db), warnings=warnings)


def _fresh_stats(db) -> dict:
    """Recompute MonitorStats from the remaining rows (post-reset snapshot).

    Mirrors execution.monitor_snapshot's stats block so the numbers the UI
    shows right after the reset match the next /monitor refresh exactly.
    """
    from app.models.schemas import MonitorStats

    rows = db.select("paper_trades", limit=500)
    closed_rows = [r for r in rows
                   if r.get("status") == "closed" and r.get("pnl") is not None]
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_ago = now - timedelta(days=7)

    def created(r: dict):
        raw = r.get("created_at")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    today_rows = [r for r in rows
                  if (c := created(r)) and c.date().isoformat() == today]
    week_rows = [r for r in rows
                 if (c := created(r)) and c >= week_ago
                 and r.get("status") != "rejected"]
    wins = [r for r in closed_rows if float(r.get("pnl") or 0) > 0]
    stats = MonitorStats(
        trades_today=len(today_rows),
        trades_week=len(week_rows),
        open_positions=len([r for r in rows if r.get("status") == "open"]),
        closed_count=len(closed_rows),
        win_rate=round(len(wins) / len(closed_rows) * 100, 1) if closed_rows else 0.0,
        pnl_today=round(sum(float(r.get("pnl") or 0) for r in today_rows), 2),
        pnl_week=round(sum(float(r.get("pnl") or 0) for r in week_rows), 2),
        pnl_total=round(sum(float(r.get("pnl") or 0) for r in closed_rows), 2),
    )
    return stats.model_dump()


# ------------------------------------------------------------- close all
class CloseAllRequest(BaseModel):
    confirm: bool = False
    close_reason: str = "close_all"


class CloseAllResult(BaseModel):
    ok: bool
    closed: int
    failed: int
    total_pnl: float = 0.0
    results: list[dict] = []
    message: str = ""


@router.post("/positions/close-all", response_model=CloseAllResult)
async def close_all_positions(payload: CloseAllRequest,
                              request: Request) -> CloseAllResult:
    """Close EVERY open paper position (monitor page ปิดทั้งหมด button).

    Reuses the single-close flow per ticket: live mark → broker close →
    PnL → journal → signal_log → notify. One LINE summary at the end
    instead of N pushes.
    """
    import logging
    from types import SimpleNamespace

    from app.services.notification_service import NotificationService

    log = logging.getLogger(__name__)
    db = request.app.state.db
    broker = request.app.state.broker

    if not payload.confirm:
        return CloseAllResult(ok=False, closed=0, failed=0,
                              message="ต้องยืนยัน (confirm=true) ก่อนปิดทั้งหมด")

    open_rows = db.select("paper_trades", filters={"status": "open"}, limit=100)
    if not open_rows:
        return CloseAllResult(ok=True, closed=0, failed=0,
                              message="ไม่มีไม้ที่เปิดค้างอยู่")

    # one live-mark batch for all assets (single spot feed round-trip)
    assets = sorted({str(r.get("asset") or "").upper() for r in open_rows})
    marks: dict[str, float] = {}
    try:
        marks, _failures = await _spot_prices(assets)
    except Exception as exc:
        log.warning("close-all: live marks unavailable: %s", exc)

    results: list[dict] = []
    closed = failed = 0
    total_pnl = 0.0
    for row in open_rows:
        ticket = str(row.get("ticket") or "")
        asset = str(row.get("asset") or "").upper()
        entry = float(row.get("entry_price") or 0)
        exit_price = float(marks.get(asset) or 0)
        if not exit_price:
            try:
                exit_price = float(await broker.mark_price(ticket))
            except Exception:
                exit_price = 0.0
        if not exit_price:
            try:
                exit_price = float(await broker.quote(asset))
            except Exception:
                exit_price = 0.0
        if not exit_price:
            exit_price = entry  # last resort: flat PnL

        result = await broker.close_position(ticket)
        if not result.ok:
            failed += 1
            results.append({"ticket": ticket, "asset": asset, "ok": False,
                            "message": result.message})
            continue

        pos = SimpleNamespace(
            direction=str(row.get("direction") or "BUY").upper(),
            current_price=exit_price, entry_price=entry,
            volume=float(row.get("volume") or 0), asset=asset)
        pnl = round(execution.PaperBrokerPnl.compute(pos), 2)
        execution.close_trade_rows(db, ticket, exit_price, pnl,
                                   payload.close_reason)
        signal_log.log_event(
            db=db, event="closed", asset=asset,
            direction=str(row.get("direction") or ""), entry=entry,
            exit_price=exit_price, pnl=pnl, ticket=ticket, source="user",
            reason=f"ปิดทั้งหมด ({payload.close_reason}) @ {exit_price:g}")
        total_pnl += pnl
        closed += 1
        results.append({"ticket": ticket, "asset": asset, "ok": True,
                        "pnl": pnl, "exit_price": exit_price})

    warnings: list[str] = []
    try:
        notifier = NotificationService(db, request.app.state.line)
        await notifier.notify(
            "", "trade_closed",
            f"✋ Close All\nปิด {closed} ไม้ (ล้มเหลว {failed}) — "
            f"PnL รวม {total_pnl:+,.2f} USD",
        )
        warnings.append("notify ok")
    except Exception as exc:
        warnings.append(f"notify failed: {exc}")

    log.info("close-all: closed=%d failed=%d pnl=%.2f", closed, failed, total_pnl)
    return CloseAllResult(
        ok=failed == 0, closed=closed, failed=failed,
        total_pnl=round(total_pnl, 2), results=results,
        message=(f"ปิดทั้งหมดแล้ว {closed} ไม้ (ล้มเหลว {failed}) — "
                 f"PnL รวม {total_pnl:+,.2f} USD")
        if closed or failed else "ไม่มีไม้ที่เปิดค้างอยู่")


# ------------------------------------------------------------- close group
class CloseGroupRequest(BaseModel):
    """ปิดไม้เป็นกลุ่มตามผลลัพธ์ (monitor ปิดกำไร/ปิดขาดทุน buttons)."""
    confirm: bool = False
    # "profit" = ปิดเฉพาะไม้ที่กำไร, "loss" = เฉพาะไม้ที่ขาดทุน
    group: str = "profit"
    close_reason: str = "close_group"


@router.post("/positions/close-group", response_model=CloseAllResult)
async def close_group_positions(payload: CloseGroupRequest,
                                request: Request) -> CloseAllResult:
    """Close open paper positions filtered by unrealized result.

    Same flow as close-all (live mark → broker close → PnL → journal →
    signal_log → one LINE summary) but only for tickets whose live mark
    puts them in profit (group="profit") or loss (group="loss").
    """
    import logging
    from types import SimpleNamespace

    from app.services.notification_service import NotificationService

    log = logging.getLogger(__name__)
    db = request.app.state.db
    broker = request.app.state.broker

    if not payload.confirm:
        return CloseAllResult(ok=False, closed=0, failed=0,
                              message="ต้องยืนยัน (confirm=true) ก่อนปิดกลุ่ม")
    if payload.group not in ("profit", "loss"):
        return CloseAllResult(ok=False, closed=0, failed=0,
                              message="group ต้องเป็น 'profit' หรือ 'loss'")

    open_rows = db.select("paper_trades", filters={"status": "open"}, limit=100)
    if not open_rows:
        return CloseAllResult(ok=True, closed=0, failed=0,
                              message="ไม่มีไม้ที่เปิดค้างอยู่")

    # one live-mark batch for all assets — needed BEFORE filtering so the
    # profit/loss split uses the same mark the close will settle at
    assets = sorted({str(r.get("asset") or "").upper() for r in open_rows})
    marks: dict[str, float] = {}
    try:
        marks, _failures = await _spot_prices(assets)
    except Exception as exc:
        log.warning("close-group: live marks unavailable: %s", exc)

    def _mark_for(row: dict) -> float:
        asset = str(row.get("asset") or "").upper()
        px = float(marks.get(asset) or 0)
        return px

    def _unrealized(row: dict) -> float:
        """Unrealized PnL sign-proxy at the live mark (0 = no mark → skip)."""
        m = _mark_for(row)
        if not m:
            return 0.0
        entry = float(row.get("entry_price") or 0)
        direction = str(row.get("direction") or "").upper()
        if entry <= 0:
            return 0.0
        diff = m - entry
        return diff if direction == "BUY" else -diff

    # filter by unrealized result at the live mark (rows without a mark are
    # excluded from both groups to avoid closing on stale data)
    if payload.group == "profit":
        targets = [r for r in open_rows if _unrealized(r) > 0]
    else:
        targets = [r for r in open_rows if _unrealized(r) < 0]
    if not targets:
        label = "กำไร" if payload.group == "profit" else "ขาดทุน"
        return CloseAllResult(ok=True, closed=0, failed=0,
                              message=f"ไม่มีไม้ที่{label}อยู่")

    results: list[dict] = []
    closed = failed = 0
    total_pnl = 0.0
    for row in targets:
        ticket = str(row.get("ticket") or "")
        asset = str(row.get("asset") or "").upper()
        entry = float(row.get("entry_price") or 0)
        exit_price = float(marks.get(asset) or 0)
        if not exit_price:
            try:
                exit_price = float(await broker.mark_price(ticket))
            except Exception:
                exit_price = 0.0
        if not exit_price:
            try:
                exit_price = float(await broker.quote(asset))
            except Exception:
                exit_price = 0.0
        if not exit_price:
            exit_price = entry  # last resort: flat PnL

        result = await broker.close_position(ticket)
        if not result.ok:
            failed += 1
            results.append({"ticket": ticket, "asset": asset, "ok": False,
                            "message": result.message})
            continue

        pos = SimpleNamespace(
            direction=str(row.get("direction") or "BUY").upper(),
            current_price=exit_price, entry_price=entry,
            volume=float(row.get("volume") or 0), asset=asset)
        pnl = round(execution.PaperBrokerPnl.compute(pos), 2)
        execution.close_trade_rows(db, ticket, exit_price, pnl,
                                   payload.close_reason)
        signal_log.log_event(
            db=db, event="closed", asset=asset,
            direction=str(row.get("direction") or ""), entry=entry,
            exit_price=exit_price, pnl=pnl, ticket=ticket, source="user",
            reason=f"ปิด{'กำไร' if payload.group == 'profit' else 'ขาดทุน'} ({payload.close_reason}) @ {exit_price:g}")
        total_pnl += pnl
        closed += 1
        results.append({"ticket": ticket, "asset": asset, "ok": True,
                        "pnl": pnl, "exit_price": exit_price})

    warnings: list[str] = []
    try:
        notifier = NotificationService(db, request.app.state.line)
        label = "Close Profit" if payload.group == "profit" else "Close Loss"
        await notifier.notify(
            "", "trade_closed",
            f"✋ {label}\nปิด {closed} ไม้ (ล้มเหลว {failed}) — "
            f"PnL รวม {total_pnl:+,.2f} USD",
        )
        warnings.append("notify ok")
    except Exception as exc:
        warnings.append(f"notify failed: {exc}")

    log.info("close-group(%s): closed=%d failed=%d pnl=%.2f",
             payload.group, closed, failed, total_pnl)
    return CloseAllResult(
        ok=failed == 0, closed=closed, failed=failed,
        total_pnl=round(total_pnl, 2), results=results,
        message=(f"ปิด{'กำไร' if payload.group == 'profit' else 'ขาดทุน'}แล้ว "
                 f"{closed} ไม้ (ล้มเหลว {failed}) — "
                 f"PnL รวม {total_pnl:+,.2f} USD")
        if closed or failed else "ไม่มีไม้ที่เปิดค้างอยู่")


# ------------------------------------------------------------- equity curve
@router.get("/equity-curve")
async def equity_curve(request: Request, days: int = 90) -> dict:
    """Equity snapshots for the performance page chart.

    One point per UTC day from equity_snapshots (written by the portfolio
    monitor worker). Falls back to a synthetic flat line at settings
    capital when the table is empty (fresh install / worker not yet run).
    """
    db = request.app.state.db
    s = _settings(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    try:
        rows = db.select("equity_snapshots", order="snapshot_date", desc=False,
                         limit=400)
    except Exception:
        rows = []
    points = [
        {"date": str(r.get("snapshot_date") or ""),
         "equity": float(r.get("equity") or 0)}
        for r in rows if str(r.get("snapshot_date") or "") >= cutoff
    ]
    if not points:
        points = [{"date": datetime.now(timezone.utc).date().isoformat(),
                   "equity": s.capital}]
    peak = max((p["equity"] for p in points), default=s.capital)
    latest = points[-1]["equity"]
    return {
        "points": points,
        "latest_equity": latest,
        "peak_equity": peak,
        "drawdown_pct": round((peak - latest) / peak * 100, 2) if peak else 0.0,
        "capital": s.capital,
        "synthetic": len(points) == 1 and points[0]["equity"] == s.capital,
    }


# ------------------------------------------------------------- signal report
@router.get("/signal-report")
async def signal_report(request: Request, days: int = 30) -> dict:
    """Signal quality report: join signal_logs (created) ↔ paper_trades.

    Joins on signal_id (paper_trades.signal_id = signal_logs.signal_id) —
    the old version joined on ticket, but `created` rows NEVER carry a
    ticket (the scanner logs created before any order exists; the ticket
    only appears on order_opened). So matched_trades was always 0. Ticket
    is kept as a legacy fallback for rows predating signal_id logging.

    Regime comes from the `signals` row linked by signal_id (paper_trades
    and signal_logs have no regime column); unknown when the row expired.
    """
    db = request.app.state.db
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        logs = [l for l in db.select("signal_logs", filters={"event": "created"},
                                     limit=500)
                if str(l.get("created_at") or "") >= cutoff]
    except Exception:
        logs = []
    try:
        trades = db.select_paged("paper_trades")
    except Exception:
        try:
            trades = db.select("paper_trades", limit=500)
        except Exception:
            trades = []
    closed = [t for t in trades if t.get("status") == "closed"]
    by_signal_id: dict[str, dict] = {}
    for t in closed:
        sid = str(t.get("signal_id") or "")
        if sid and sid not in by_signal_id:
            by_signal_id[sid] = t
    by_ticket = {str(t.get("ticket") or ""): t for t in closed if t.get("ticket")}
    # regime lookup: latest market_analysis row per asset (neither
    # paper_trades nor signal_logs stores a regime column; the old code
    # read t.regime/l.regime which never exist → always "unknown")
    try:
        market_rows = db.select("market_analysis", limit=200)
    except Exception:
        market_rows = []
    regime_by_asset: dict[str, str] = {}
    for r in market_rows:
        asset_key = str(r.get("asset") or "").upper()
        if asset_key and asset_key not in regime_by_asset:
            regime_by_asset[asset_key] = str(r.get("regime") or "")

    def band(conf: float) -> str:
        if conf >= 90:
            return "90+"
        if conf >= 80:
            return "80-89"
        if conf >= 70:
            return "70-79"
        return "<70"

    by_asset: dict[str, list[dict]] = {}
    by_band: dict[str, list[dict]] = {}
    by_regime: dict[str, list[dict]] = {}
    matched = 0
    for l in logs:
        # Primary join: signal_id (durable — logged by both scanner and
        # execution). Legacy fallback: ticket, for rows predating it.
        sid = str(l.get("signal_id") or "")
        t = by_signal_id.get(sid) if sid else None
        if t is None:
            ticket = str(l.get("ticket") or "")
            t = by_ticket.get(ticket) if ticket else None
        if not t:
            continue
        matched += 1
        asset = str(t.get("asset") or l.get("asset") or "")
        pnl = float(t.get("pnl") or 0)
        rec = {"asset": asset, "pnl": pnl,
               "confidence": float(l.get("confidence") or 0),
               "regime": regime_by_asset.get(asset.upper(), "")}
        by_asset.setdefault(rec["asset"], []).append(rec)
        by_band.setdefault(band(rec["confidence"]), []).append(rec)
        by_regime.setdefault(rec["regime"] or "unknown", []).append(rec)

    def summarize(group: dict[str, list[dict]]) -> list[dict]:
        out = []
        for key, recs in sorted(group.items()):
            wins = len([r for r in recs if r["pnl"] > 0])
            out.append({
                "key": key, "trades": len(recs),
                "win_rate_pct": round(wins / len(recs) * 100, 1) if recs else 0.0,
                "total_pnl": round(sum(r["pnl"] for r in recs), 2),
            })
        return out

    return {
        "days": days,
        "signals": len(logs),
        "matched_trades": matched,
        "by_asset": summarize(by_asset),
        "by_confidence_band": summarize(by_band),
        "by_regime": summarize(by_regime),
    }


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
    """7/30/90-day performance: win rate, profit factor, avg RR, best/worst setup.

    Reads CLOSED paper_trades rows (the live journal) — the old version read
    the legacy manual trading_journal table (migration 005, only written by
    POST /journal), so it always reported 0 while real orders closed through
    paper_trades. RR/holding are derived (paper_trades has no such columns).
    POST /journal still writes the manual table for hand-logged trades.
    """
    db = request.app.state.db
    try:
        rows = db.select_paged("paper_trades", filters={"status": "closed"})
    except Exception:
        try:
            rows = db.select("paper_trades", filters={"status": "closed"},
                             limit=500)
        except Exception:
            rows = []
    entries = _journal_entries_from_paper_trades(rows)
    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        entries = [e for e in entries
                   if str(e.closed_at or e.created_at or "")[:10] >= cutoff]
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
                              note=f"{config.asset} {config.indicator} — ไม่มีข้อมูลราคา "
                                   f"({exc.__class__.__name__}) | indicator-only long/flat "
                                   f"บน daily candles (ยังไม่รวม confidence gate / risk sizing / "
                                   f"spread ของระบบจริง)")
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
                                 note=f"{config.asset} {config.indicator} — ไม่มีข้อมูลราคา "
                                      f"({exc.__class__.__name__}) | indicator-only long/flat "
                                      f"บน daily candles (ยังไม่รวม confidence gate / risk sizing / "
                                      f"spread ของระบบจริง)")
    return walk_forward(candles, config)


# ----------------------------------------------------------- paper trading
@router.get("/paper-trading", response_model=PaperTradingStatus)
async def paper_trading(request: Request) -> PaperTradingStatus:
    """Virtual capital status + AI coaching + live readiness score.

    DB-backed: PnL/open counts come from paper_trades (durable) — the old
    version read the in-memory PaperBroker book (closed_trades/_positions),
    so every Render redeploy wiped the history and readiness reset to 0
    even though the journal rows were still in the DB. Falls back to the
    broker book only when the DB read fails.
    """
    from types import SimpleNamespace

    db = request.app.state.db
    broker = request.app.state.broker
    s = _settings(request)
    try:
        try:
            rows = db.select_paged("paper_trades")
        except Exception:
            rows = db.select("paper_trades", limit=500)
        closed_pnls = [float(r.get("pnl") or 0) for r in rows
                       if r.get("status") == "closed" and r.get("pnl") is not None]
        open_count = len([r for r in rows if r.get("status") == "open"])
        book = SimpleNamespace(
            closed_trades=[{"pnl": p} for p in closed_pnls],
            _positions={f"DB-{i}": 1 for i in range(open_count)},
        )
        return paper_trading_status(book, virtual_capital=s.paper_virtual_capital)
    except Exception:
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

    # market snapshot for the order-strategy leg — TOP SCORER, same source
    # as the market header + goal assessment + chat context (never the
    # newest row: limit=5 + rows[0] picked an arbitrary asset, so Extended
    # disagreed with every other surface). limit=50 keeps the full ~28-row
    # scanner cycle; dedupe keeps the newest row per asset.
    try:
        _rows = db.select("market_analysis", limit=50) or []
    except Exception:
        _rows = []
    _seen: set[str] = set()
    _per_asset: dict[str, tuple[float, str]] = {}
    for _r in _rows:
        _a = str(_r.get("asset") or "").upper()
        if not _a or _a in _seen:
            continue  # select() is newest-first → keep newest row per asset
        _seen.add(_a)
        try:
            _score = float(_r.get("confidence") or 0)
        except (TypeError, ValueError):
            _score = 0.0
        _per_asset[_a] = (_score, str(_r.get("regime") or "sideway"))
    if _per_asset:
        asset, (_conf, regime) = max(_per_asset.items(), key=lambda kv: kv[1][0])
        confidence = float(_conf or 0)
    else:
        asset, regime, confidence = "EURUSD", "sideway", 0.0

    # Frequency aligned with the execution gate (same counting + bull_trend
    # bypass + per-asset quality bar) so FINAL can reach TRADE on a clean
    # trend top-scorer. The old get_frequency() defaulted regime=sideway →
    # always throttled → officer REJECTED → FINAL always WAIT, so the
    # open button could never fire even though /approve opened fine.
    freq = _evaluate_frequency(db, s, confidence=confidence, asset=asset)
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
        correlation_cap=s.correlation_cap,
        min_confidence=effective_min_confidence(s, asset),
        min_opportunity=s.min_opportunity)

    # order strategy — REAL proposal for the top scorer (same path as the
    # scanner: live snapshot → opportunity score → build_proposal with the
    # user's RR target + SL clamp + gold invalidation → OrderStrategyEngine
    # legs). The old code priced a dummy BUY 1.0/0.99/1.02 on every asset,
    # so gold showed a 1.0 entry and FX-sized legs. Fail-safe: snapshot
    # failure falls back to a spot-anchored plan, never raises.
    from app.engine.strategy_engine import regime_of as _regime_of
    _direction: str = "BUY"
    _atr = 0.8
    _entry = 0.0
    _sl = 0.0
    _tp = 0.0
    try:
        from app.integrations import quotes as _quotes
        _snaps = await _quotes.fetch_all_snapshots([asset])
        _snap = dict((_snaps or {}).get(asset) or {})
    except Exception:
        _snap = {}
    if _snap:
        try:
            _ind = IndicatorSnapshot(**{**_snap, "source": "live"})
            _opp = engine.opportunity_score(_ind)
            _bullish = bool(_ind.ema_fast > _ind.ema_slow)
            _rr = max(0.5, float(getattr(s, "rr_target", 2.0) or 2.0))
            _prop = engine.build_proposal(
                _ind, _opp, risk_per_trade_pct=float(s.risk_per_trade_pct or 0),
                regime_bullish=_bullish, rr_target=_rr,
                sl_min_pct=float(getattr(s, "sl_distance_min_pct", 0) or 0),
                sl_max_pct=float(getattr(s, "sl_distance_max_pct", 0) or 0),
                invalidation_level=(float(_ind.breakout_level)
                                    if str(asset).upper() == "XAUUSD" else 0.0))
            _direction = str(_prop.direction or "BUY").upper()
            _atr = float(_ind.atr_pct or 0.8)
            _entry = float(_prop.entry or 0)
            _sl = float(_prop.stop_loss or 0)
            _tp = float(_prop.take_profit or 0)
            confidence = float(_prop.confidence or confidence)
            regime = _regime_of(_ind)
        except Exception:
            pass
    if not _entry:
        # snapshot unavailable — anchor at the live spot price with an
        # ATR-derived stop so the plan still reflects the real market.
        try:
            from app.integrations import quotes as _quotes2
            _prices, _fail = await _quotes2.fetch_spot_prices([asset])
            _spot = float((_prices or {}).get(asset) or 0)
        except Exception:
            _spot = 0.0
        if _spot > 0:
            _entry = _spot
            _dist = max(_spot * _atr / 100.0 * 1.5, _spot * 0.001)
            _direction = "BUY" if regime in ("bull_trend", "strong_bull_trend") else "SELL"
            _sign = 1.0 if _direction == "BUY" else -1.0
            _rr = max(0.5, float(getattr(s, "rr_target", 2.0) or 2.0))
            _sl = _spot - _sign * _dist
            _tp = _spot + _sign * _dist * _rr
    plan = OrderStrategyEngine().build_plan(
        asset=asset, direction=_direction,  # type: ignore[arg-type]
        entry=_entry or 1.0, stop_loss=_sl or (_entry or 1.0) * 0.99,
        take_profit=_tp or (_entry or 1.0) * 1.02,
        regime=regime, atr_pct=_atr,
        equity=s.capital,
        # live risk %, NOT freq.limits.risk_per_trade_pct — that is Optional and
        # becomes None the moment the frequency gate denies (limits exhausted),
        # which silently re-priced every leg at the 1.0% fallback: the panel
        # showed a lot up to 6x smaller than the one execution would use.
        # _evaluate_frequency always overrides limits with s.risk_per_trade_pct,
        # so reading the setting directly is both safer and identical.
        risk_per_trade_pct=float(s.risk_per_trade_pct or 1.0))
    # re-evaluate frequency + officer against the REAL proposal confidence
    # so the review/final decision match the plan shown (the old flow
    # reviewed the scanner-row score but displayed dummy BUY legs).
    freq = _evaluate_frequency(db, s, confidence=confidence, asset=asset)
    officer = RiskOfficer().review_trade(
        confidence=confidence, opportunity_score=confidence,
        frequency=freq, news_risk=news, kill_switch=ks,
        correlation_score=corr["portfolio_correlation"],
        correlation_cap=s.correlation_cap,
        min_confidence=effective_min_confidence(s, asset),
        min_opportunity=s.min_opportunity)

    # backtest leg — LIVE run for the same top scorer (fail-soft). The old
    # text told the user to POST manually, so Extended never showed a real
    # number. Uses the saved backtest_* settings (same defaults the panel
    # seeds) over real daily candles; feed failure keeps an honest note.
    _bt_text = "ยิง POST /api/trading/backtest เพื่อรันตาม indicator (ไม่รันอัตโนมัติเพราะใช้เวลา)"
    try:
        from app.integrations import quotes as _quotes3
        from app.models.schemas import BacktestConfig as _BTC
        from app.models.schemas import INDICATORS as _INDS
        from app.models.schemas import run_backtest as _run_bt
        import httpx as _httpx
        _bt_ind = str(getattr(s, "backtest_indicator", "EMA") or "EMA")
        if _bt_ind not in list(_INDS):
            _bt_ind = "EMA"
        _bt_days = int(getattr(s, "backtest_days", 120) or 120)
        _bt_cfg = _BTC(asset=asset, indicator=_bt_ind,  # type: ignore[arg-type]
                       days=max(30, min(_bt_days, 365)),
                       initial_capital=float(s.capital or 0),
                       risk_per_trade_pct=float(s.risk_per_trade_pct or 0))
        async with _httpx.AsyncClient() as _client:
            _candles = await _quotes3.fetch_candles(
                asset, _client, days=int(_bt_cfg.days))
        _bt_res = _run_bt(_candles, _bt_cfg)
        _bt_text = (f"{asset} {_bt_ind} {_bt_days}d: {_bt_res.total_trades} trades, "
                    f"win {_bt_res.win_rate_pct}%, PF {_bt_res.profit_factor}, "
                    f"Sharpe {_bt_res.sharpe_ratio}, MaxDD {_bt_res.max_drawdown_pct}%, "
                    f"equity {_bt_res.final_equity:g} — {_bt_res.note}")
    except Exception as _bt_exc:
        _bt_text = (f"{asset}: รัน backtest ไม่สำเร็จ ({_bt_exc.__class__.__name__}) — "
                    f"ยิง POST /api/trading/backtest เพื่อรันตาม indicator")

    return {
        "asset": asset,
        "confidence": confidence,
        "direction": _direction,
        "regime": regime,
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
        "backtest_result": _bt_text,
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
