"""Execution layer — the ONE gate pipeline every order must pass.

Used by both the semi-auto /approve endpoint and the AutoTrader worker, so
the two paths can never drift apart:

    pause → kill switch → frequency → news → correlation → risk officer
    → position sizing (risk_to_lot) → broker.place_order → journal row

No order fires without a GateReport.allowed verdict. The gate defaults to
BLOCKING on engine errors (fail-safe): if a check cannot run, the trade is
refused, not waved through.
"""
from __future__ import annotations

import inspect
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from app.api.routes.settings import get_app_settings
from app.integrations.brokers import OrderRequest
from app.integrations import quotes
from app.models.schemas import (
    AppSettings,
    EconomicCalendarEngine,
    EconomicEvent,
    FrequencyEngine,
    GateReport,
    KillSwitchEngine,
    KillSwitchStatus,
    MonitorSnapshot,
    NewsRiskStatus,
    PauseStatus,
    RiskOfficer,
    RiskProfile,
    TradeLimits,
    contract_value_for,
    effective_min_confidence,
    effective_min_lot,
    effective_spread,
    risk_to_lot,
    risk_to_lot_for,
)

from app.services import signal_log

log = logging.getLogger(__name__)

# Fixed demo user until multi-user auth lands (same id /approve already used).
DEFAULT_USER = "demo"

# Pending signals older than this leave the queue (marked 'expired') no matter
# which order_mode the platform is in — otherwise the signals page shows
# yesterday's entry prices forever in semi_auto/manual modes.
SIGNAL_TTL_MIN = 30


def expire_stale_pending_signals(db) -> int:
    """Mark pending signals older than SIGNAL_TTL_MIN as expired.

    Safe to call on every /signals/latest request — updates only rows that
    are actually stale, and degrades to 'rejected' when the DB lacks the
    009 migration's 'expired' enum value. Returns the number expired.
    """
    if not db or not getattr(db, "available", False) \
            or not callable(getattr(db, "select", None)):
        return 0
    now = datetime.now(timezone.utc)
    expired = 0
    try:
        pending = db.select("signals", filters={"approval": "pending"}, limit=200)
    except Exception:
        return 0
    for sig in pending:
        created = str(sig.get("created_at") or "")
        if not created:
            continue
        dt = _parse_dt(created)
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if (now - dt).total_seconds() / 60 > SIGNAL_TTL_MIN:
            if not db.update("signals", sig["id"], {"approval": "expired"}):
                db.update("signals", sig["id"], {"approval": "rejected"})
            expired += 1
            # Lifecycle log: pending past the 30-min TTL → never became an order.
            signal_log.log_event(
                db=db, event="expired", signal_id=str(sig.get("id") or ""),
                asset=str(sig.get("asset") or ""),
                direction=str(sig.get("direction") or ""),
                confidence=sig.get("confidence"), entry=sig.get("entry"),
                source="scanner",
                reason=(f"สัญญาณนี้ pending เกิน {SIGNAL_TTL_MIN} นาที — "
                        "หมดอายุ ไม่ได้ใช้เปิดออเดอร์"))
    return expired


def is_stale(row: dict, max_age_min: int = SIGNAL_TTL_MIN) -> bool:
    """True when the row has a parseable created_at older than max_age_min.

    Rows without a parseable created_at (legacy/test data) are never stale —
    callers keep showing them rather than silently dropping history.
    """
    dt = _parse_dt(str(row.get("created_at") or ""))
    if dt is None:
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60 > max_age_min


def now_iso() -> str:
    """UTC timestamp for *_at stamps (signals.approved_at etc.)."""
    return datetime.now(timezone.utc).isoformat()


JOURNAL_INSERT = {
    "asset": "XAUUSD",  # sentinel replaced per trade; keeps insert contract explicit
}


# ---------------------------------------------------------------------------
# Trading-pause state (real kill switch)
# ---------------------------------------------------------------------------
def get_pause(db) -> PauseStatus:
    """Read the single trading_pause row (missing table/row → not paused)."""
    if not db or not getattr(db, "available", False) or not hasattr(db, "_client"):
        return PauseStatus()
    try:
        resp = db._client.table("trading_pause").select("*").eq("id", 1).limit(1).execute()
        rows = list(resp.data or [])
        if not rows:
            return PauseStatus()
        r = rows[0]
        return PauseStatus(
            paused=bool(r.get("paused")), reason=str(r.get("reason") or ""),
            paused_at=r.get("paused_at"),
        )
    except Exception as exc:
        log.error("get_pause failed: %s", exc)
        return PauseStatus()


def set_pause(db, paused: bool, reason: str = "") -> PauseStatus:
    """Upsert the trading_pause row (LINE /pause, /resume, API + UI)."""
    now = datetime.now(timezone.utc).isoformat()
    if db and getattr(db, "available", False) and hasattr(db, "_client"):
        try:
            db._client.table("trading_pause").upsert({
                "id": 1, "paused": paused, "reason": reason,
                "paused_at": now if paused else None, "updated_at": now,
            }).execute()
        except Exception as exc:
            log.error("set_pause failed: %s", exc)
    return PauseStatus(paused=paused, reason=reason,
                       paused_at=now if paused else None)


# ---------------------------------------------------------------------------
# Paper trade journal
# ---------------------------------------------------------------------------
def record_trade(db, trade: dict[str, Any]) -> None:
    """Insert one execution row into paper_trades (never raises)."""
    # Snapshot the levels the position was OPENED with — the monitor page
    # compares current SL/TP against these to badge moved levels (migration
    # 021). Missing keys (legacy callers/tests) just stay None.
    trade.setdefault("initial_stop_loss", trade.get("stop_loss"))
    trade.setdefault("initial_take_profit", trade.get("take_profit"))
    try:
        db.insert("paper_trades", trade)
    except Exception as exc:  # pragma: no cover — Database.insert already swallows
        log.error("record_trade failed: %s", exc)


def persist_sl_move(db, ticket: str, new_sl: float, reason: str) -> None:
    """Write a guard SL move back to the open paper_trades row.

    The guard moves the SL on the broker book (breakeven → trailing), but
    without this write the journal kept the ORIGINAL stop_loss — the monitor
    showed a stale SL and there was no way to badge "SL ถูกขยับ" (migration
    021). Never raises: a failed write only costs the badge, never the trade.
    """
    try:
        rows = db.select("paper_trades",
                         filters={"ticket": str(ticket or ""), "status": "open"},
                         limit=1)
        if rows:
            db.update("paper_trades", rows[0]["id"], {
                "stop_loss": round(float(new_sl), 5),
                "sl_moved_at": now_iso(),
                "sl_move_reason": str(reason or "")[:200],
            })
    except Exception as exc:
        log.error("persist_sl_move failed for %s: %s", ticket, exc)


def persist_tp_move(db, ticket: str, new_tp: float, reason: str) -> None:
    """Write a TP move back to the open paper_trades row (same as SL)."""
    try:
        rows = db.select("paper_trades",
                         filters={"ticket": str(ticket or ""), "status": "open"},
                         limit=1)
        if rows:
            db.update("paper_trades", rows[0]["id"], {
                "take_profit": round(float(new_tp), 5),
                "tp_moved_at": now_iso(),
                "tp_move_reason": str(reason or "")[:200],
            })
    except Exception as exc:
        log.error("persist_tp_move failed for %s: %s", ticket, exc)


def close_trade_rows(db, ticket: str, exit_price: float, pnl: float,
                     reason: str) -> None:
    """Mark the matching open paper_trades row closed after SL/TP/manual exit."""
    try:
        rows = db.select("paper_trades", filters={"ticket": ticket, "status": "open"},
                         limit=1)
        if rows:
            db.update("paper_trades", rows[0]["id"], {
                "status": "closed", "exit_price": exit_price,
                "pnl": round(pnl, 2), "close_reason": reason,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as exc:
        log.error("close_trade_rows failed: %s", exc)


class PaperBrokerPnl:
    """PnL helper shared with the position guard (mirrors PaperBroker math).

    Volume is in lots: FX standard lot = 100,000 units, XAUUSD = 100 oz.
    Without the contract multiplier a 0.01-lot FX move of 100 pips would
    report PnL 0.10 instead of 100.00 — the monitor page showed PnL stuck
    at 0.00 because of this.
    """

    CONTRACT_SIZES = {"XAUUSD": 100.0}  # everything else defaults to FX 100k

    @staticmethod
    def compute(pos) -> float:
        sign = 1 if pos.direction == "BUY" else -1
        asset = str(getattr(pos, "asset", "") or "").upper()
        contract = PaperBrokerPnl.CONTRACT_SIZES.get(asset, 100_000.0)
        return sign * (pos.current_price - pos.entry_price) * pos.volume * contract


# ---------------------------------------------------------------------------
# News gate (calendar rows → NewsRiskStatus)
# ---------------------------------------------------------------------------
def _news_risk(db, s: AppSettings) -> NewsRiskStatus:
    try:
        rows = db.select("economic_calendar", limit=50)
    except Exception:
        rows = []
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
    return EconomicCalendarEngine(block_minutes=s.news_block_minutes).news_risk(events, now)


# ---------------------------------------------------------------------------
# Loss percentages for the kill switch (from paper_trades journal)
# ---------------------------------------------------------------------------
def _loss_pcts(db, capital: float) -> tuple[float, float, float]:
    """(daily, weekly, monthly) loss % — positive number = losing money."""
    try:
        rows = db.select("paper_trades", filters={"status": "closed"}, limit=500)
    except Exception:
        rows = []
    now = datetime.now(timezone.utc)
    cutoff_day = now - timedelta(days=1)
    cutoff_week = now - timedelta(days=7)
    cutoff_month = now - timedelta(days=30)

    def pnl_since(cut: datetime) -> float:
        total = 0.0
        for r in rows:
            raw = r.get("closed_at") or r.get("created_at")
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cut:
                total += float(r.get("pnl") or 0)
        return total

    if capital <= 0:
        return 0.0, 0.0, 0.0
    daily, weekly, monthly = (pnl_since(c) for c in (cutoff_day, cutoff_week, cutoff_month))
    return (max(0.0, -daily / capital * 100),
            max(0.0, -weekly / capital * 100),
            max(0.0, -monthly / capital * 100))


# ---------------------------------------------------------------------------
# Equity curve — daily snapshots power the REAL drawdown (kill switch)
# ---------------------------------------------------------------------------
def equity_drawdown_pct(db, capital: float) -> float:
    """Peak-to-current drawdown % from equity_snapshots (0.0 without data).

    The kill switch used to hardcode drawdown_pct=0.0 because there was no
    equity history; with daily snapshots (portfolio_monitor writes one per
    cycle) the drawdown gate finally works. Falls back to 0.0 when the table
    is missing/empty so old DBs keep behaving as before.

    Ordering is by snapshot_date (NOT created_at — the monitor rewrites
    today's row in place so created_at order can put a stale row first).
    Stale peaks from an older capital regime are clamped: snapshots above
    3x current capital are ignored so a 10k→100 capital change can't read
    99% drawdown forever (prod 2026-09-09: peak 10k vs equity ~100).
    """
    if not db or not getattr(db, "available", False) or capital <= 0:
        return 0.0
    try:
        rows = db.select("equity_snapshots", order="snapshot_date", desc=True,
                         limit=400)
    except Exception:
        return 0.0
    if not rows:
        return 0.0
    equities = [float(r.get("equity") or 0) for r in rows
                if r.get("equity") is not None]
    if not equities:
        return 0.0
    current = equities[0]  # rows are newest-first by snapshot_date
    if current <= 0:
        return 0.0
    # Ignore stale peaks from a previous capital regime (reseed/reset leaves
    # one row at the new capital; older 10k-era rows would fake 99% DD).
    sane = [v for v in equities if v <= current * 3.0] or [current]
    peak = max(sane)
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - current) / peak * 100.0)


# ---------------------------------------------------------------------------
# Avg hold — SINGLE shared definition (guard Smart Exit + monitor share this)
# ---------------------------------------------------------------------------
def avg_hold_days(db, closed_rows: list[dict] | None = None) -> float:
    """Mean open→close span in days from closed paper_trades (4.0 fallback).

    Guard used to compute this from its own query and the monitor from its
    already-fetched closed_rows — same math, two copies. Pass closed_rows
    when the caller already has them (monitor) to skip a DB round-trip.
    Never raises.
    """
    try:
        rows = closed_rows
        if rows is None:
            try:
                rows = db.select("paper_trades", filters={"status": "closed"},
                                 limit=500)
            except Exception:
                rows = []
        spans: list[float] = []
        for r in rows or []:
            c = _parse_dt(r.get("created_at"))
            x = _parse_dt(r.get("closed_at"))
            if c and x:
                spans.append(max(0.0, (x - c).total_seconds() / 86400.0))
        if spans:
            return round(sum(spans) / len(spans), 2)
    except Exception:
        pass
    return 4.0


# ---------------------------------------------------------------------------
# Peak equity — SINGLE shared definition (monitor / chat / kill share this)
# ---------------------------------------------------------------------------
def peak_equity(db, capital: float, equity: float) -> float:
    """Historical peak equity — snapshots peak, never just max(capital, equity).

    The old max(capital, equity) zeroed drawdown after a profitable run
    (e.g. 10k → 12k → 11k showed 0% instead of 8.3% from the 12k peak).
    Falls back to max(capital, equity) when the table is missing/empty.
    Never raises.
    """
    peak = max(capital, equity)
    try:
        if not db or not getattr(db, "available", False):
            return peak
        rows = db.select("equity_snapshots", limit=400)
        base = max(capital, equity)
        for r in rows or []:
            try:
                v = float(r.get("equity") or 0)
                # Same stale-regime clamp as equity_drawdown_pct: ignore
                # snapshots from an older capital era (>3x current base).
                if v > peak and v <= base * 3.0:
                    peak = v
            except Exception:
                continue
    except Exception:
        pass
    return peak


# ---------------------------------------------------------------------------
# Kill-switch — SINGLE shared evaluation (gate / guard / monitor share this)
# ---------------------------------------------------------------------------
def evaluate_kill(db, s: AppSettings,
                  broker_connected: bool = True,
                  market_data_ok: bool = True,
                  ai_provider_ok: bool = True,
                  execution_ok: bool = True) -> KillSwitchStatus:
    """One kill-switch path for the whole platform (fail-safe engaged).

    Wraps _loss_pcts + equity_drawdown_pct + KillSwitchEngine.evaluate so the
    execution gate, the position-guard emergency exit, the monitor banner,
    the /kill-switch endpoint and the goal assessment can never drift apart.
    Never raises — on any error returns engaged
    (fail-safe: halt trading when safety data is unreadable).

    Infra flags default True (gate/guard/monitor have no live health probe);
    the /kill-switch endpoint passes the real broker_connected state.
    """
    try:
        capital = float(getattr(s, "capital", 0) or 0)
        daily, weekly, monthly = _loss_pcts(db, capital)
        dd = equity_drawdown_pct(db, capital)
        return KillSwitchEngine(
            daily_loss_limit=float(getattr(s, "kill_daily_loss_pct", 2.0) or 2.0),
            weekly_loss_limit=float(getattr(s, "kill_weekly_loss_pct", 5.0) or 5.0),
            monthly_loss_limit=float(getattr(s, "kill_monthly_loss_pct", 8.0) or 8.0),
            drawdown_limit=float(getattr(s, "max_drawdown_pct", 10.0) or 10.0),
        ).evaluate(
            daily_loss_pct=daily, weekly_loss_pct=weekly,
            monthly_loss_pct=monthly, drawdown_pct=dd,
            broker_connected=broker_connected, market_data_ok=market_data_ok,
            ai_provider_ok=ai_provider_ok, execution_ok=execution_ok,
        )
    except Exception as exc:
        return KillSwitchStatus(
            engaged=True, triggers=[f"kill-switch eval error: {exc}"],
            message="kill switch unavailable — fail-safe engaged")


# ---------------------------------------------------------------------------
# The gate pipeline
# ---------------------------------------------------------------------------
def _gate_blocked(db, s: AppSettings, user_id: str, asset: str,
                  confidence: float, opportunity: float,
                  entry: Optional[float] = None,
                  stop_loss: Optional[float] = None) -> GateReport:
    """Run every safety gate. Returns GateReport with allowed=False on any block.

    size_lots is computed here too, so callers never place an un-sized order.
    entry/stop_loss feed Gate 6 (portfolio heat) with the FINAL order levels.
    """
    rejects: list[str] = []
    checks: list[str] = []

    # ---- Gate 0: manual pause switch -------------------------------------
    pause = get_pause(db)
    if pause.paused:
        rejects.append(f"Trading paused: {pause.reason or 'manual pause'}")
    checks.append(f"pause={'ENGAGED' if pause.paused else 'clear'}")

    # ---- Gate 1: kill switch (single shared path — see evaluate_kill) ----
    ks: KillSwitchStatus = evaluate_kill(db, s)
    if ks.engaged:
        rejects.append(ks.message)
    checks.append(f"kill_switch={'ENGAGED' if ks.engaged else 'clear'}")

    # ---- Gate 2: frequency (today/week/open counts) -----------------------
    freq_allowed, freq_reason = True, ""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        week_ago = (now := datetime.now(timezone.utc)) - timedelta(days=7)
        todays = db.select("paper_trades", limit=500)
        today_count = len([r for r in todays
                           if str(r.get("created_at", ""))[:10] == today
                           and r.get("status") != "rejected"])
        week_count = len([r for r in todays
                          if str(r.get("created_at", "")) >= week_ago.isoformat()[:10]
                          and r.get("status") != "rejected"])
        open_rows = db.select("paper_trades", filters={"status": "open"}, limit=100)
        open_count = len(open_rows)
        freq = FrequencyEngine(
            s.risk_profile,
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
            regime="bull_trend",  # regime gating belongs to the scanner, not the executor
            volatility_index=0.0,
        )
        freq_allowed, freq_reason = freq.allowed, freq.reason
    except Exception as exc:
        freq_allowed, freq_reason = False, f"frequency eval error: {exc}"
    if not freq_allowed:
        rejects.append(freq_reason)
    checks.append(f"frequency={'ok' if freq_allowed else freq_reason}")

    # ---- Gate 3: news block ----------------------------------------------
    news: NewsRiskStatus
    try:
        news = _news_risk(db, s)
    except Exception as exc:
        news = NewsRiskStatus(status="DANGER", reason=f"news eval error: {exc}")
    if news.status == "DANGER":
        rejects.append(f"News gate: {news.reason}")
    checks.append(f"news={news.status}")

    # ---- Gate 4: correlation cap -----------------------------------------
    corr_score, corr_reject = 0.0, ""
    try:
        open_rows = db.select("paper_trades", filters={"status": "open"}, limit=100)
        assets = sorted({r["asset"] for r in open_rows} | {asset})
        from app.models.schemas import CorrelationEngine
        corr_score = CorrelationEngine().portfolio_correlation(assets)
        if corr_score > s.correlation_cap:
            corr_reject = (f"portfolio correlation {corr_score:.0f} > cap "
                           f"{s.correlation_cap:.0f}")
    except Exception as exc:
        corr_reject = f"correlation eval error: {exc}"
    if corr_reject:
        rejects.append(f"Correlation gate: {corr_reject}")
    checks.append(f"correlation={corr_score:.0f}/cap {s.correlation_cap:.0f}")

    # ---- Gate 5: risk officer (final veto) --------------------------------
    officer = RiskOfficer().review_trade(
        confidence=confidence,
        opportunity_score=opportunity,
        frequency=type("F", (), {"allowed": freq_allowed, "reason": freq_reason})(),
        news_risk=news,
        kill_switch=ks,
        correlation_score=corr_score,
        correlation_cap=s.correlation_cap,
        # Quality bar follows the user's Settings page (incl. gold override),
        # same source the scanner used to create the signal — no drift.
        min_confidence=effective_min_confidence(s, asset),
        min_opportunity=s.min_opportunity,
    )
    if officer.verdict == "REJECTED":
        rejects.extend(officer.rejects)
    checks.append(f"risk_officer={officer.verdict}")

    # ---- Gate 6: portfolio heat (open risk + new trade vs daily budget) ---
    # Prod showed 5 open FX positions risking $52 (55% of a $100 account)
    # while the gate kept firing — kill uses REALIZED losses only, so open
    # heat never blocked. Same $ math as the monitor card (fail-safe block).
    heat_open_pct, heat_new_pct = 0.0, 0.0
    try:
        _open = db.select("paper_trades", filters={"status": "open"},
                          limit=100)
        _open_risk = 0.0
        for _t in _open or []:
            try:
                if _t.get("stop_loss") and _t.get("entry_price"):
                    _open_risk += abs(float(_t["entry_price"]) - float(_t["stop_loss"])) \
                        * float(_t.get("volume") or 0) \
                        * contract_value_for(str(_t.get("asset") or ""))
            except Exception:
                continue
        _cap = float(getattr(s, "capital", 0) or 0)
        if _cap > 0:
            heat_open_pct = _open_risk / _cap * 100.0
            if entry and stop_loss:
                try:
                    _lots = size_position(s, float(entry), float(stop_loss),
                                          asset=asset)
                    _dist = abs(float(entry) - float(stop_loss))
                    heat_new_pct = (_dist * _lots
                                    * contract_value_for(asset) / _cap * 100.0)
                except Exception:
                    heat_new_pct = 0.0
            _limit = float(getattr(s, "kill_daily_loss_pct", 2.0) or 2.0)
            if heat_open_pct + heat_new_pct > _limit:
                rejects.append(
                    f"Portfolio heat เต็ม: ไม้เปิดเสี่ยง "
                    f"{heat_open_pct:.2f}% (${_open_risk:,.2f}) + "
                    f"ไม้ใหม่ ~{heat_new_pct:.2f}% "
                    f"เกินงบ daily {_limit:g}% \u2014 "
                    f"รอปิดไม้เดิมก่อน")
        checks.append(f"heat=open {heat_open_pct:.2f}%+new ~{heat_new_pct:.2f}%")
    except Exception as exc:
        rejects.append(f"heat eval error: {exc}")
        checks.append("heat=error")

    # ---- Position sizing: risk_to_lot replaces the hardcoded 0.01 ---------
    return GateReport(allowed=not rejects, rejects=rejects, checks=checks,
                      pause=pause)


def size_position(s: AppSettings, entry: float, stop_loss: Optional[float],
                  asset: Optional[str] = None) -> float:
    """risk_per_trade_pct of settings.capital → lots (risk_to_lot).

    The result is floored at the effective min_lot (Settings page, default
    0.01) so tiny accounts still open a visible size — gold (XAUUSD) can use
    its own Min Lot (gold) override, every other asset uses the base min_lot.
    Sizing uses the per-asset contract value (gold = 100 oz/lot, FX = 100k
    units/lot) so the risk budget converts to a realistic volume.
    """
    if not entry or not stop_loss:
        return 0.0
    stop_distance = abs(entry - stop_loss)
    lots = risk_to_lot_for(s.capital, s.risk_per_trade_pct, stop_distance,
                           asset or "")
    return max(lots, effective_min_lot(s, asset))


def apply_spread(entry: float, direction: str, spread: float) -> float:
    """Paper-fill price with a simulated spread (realism for paper PnL).

    BUYs fill at entry + spread/2 (pay the ask), SELLs at entry − spread/2
    (pay the bid). spread=0 → fill exactly at the mid price (old behaviour).
    """
    if not spread or spread <= 0:
        return entry
    half = spread / 2.0
    return round(entry + half, 5) if str(direction).upper() == "BUY" \
        else round(entry - half, 5)


# ---------------------------------------------------------------------------
# Entry points — used by /approve AND the auto trader
# ---------------------------------------------------------------------------
async def execute_signal(db, broker, notifier, s: AppSettings, *,
                         user_id: str, asset: str, direction: str,
                         entry: float, stop_loss: Optional[float],
                         take_profit: Optional[float], confidence: float,
                         opportunity: float, signal_id: Optional[str],
                         source: str) -> GateReport:
    """Gate → size → place order → journal → notify. The single execution path."""
    # ---- Live-price re-anchor (2026-09-08) --------------------------------
    # The stored signal row can be up to SIGNAL_TTL_MIN (30 min) old, so its
    # entry price may no longer be the market price when the order actually
    # fires. Fetch the intraday spot (Yahoo, same feed the scanner uses to
    # re-anchor cards) and shift entry/SL/TP proportionally so the REAL order
    # starts from the CURRENT price — not the price on the card. Fail-safe:
    # any feed error keeps the signal prices (old behaviour) rather than
    # blocking the trade.
    reanchor_note = ""
    if entry and entry > 0:
        try:
            spot, _spot_fail = await quotes.fetch_spot_prices([asset])
            live_price = float(spot.get(asset) or 0)
        except Exception as exc:
            log.warning("live re-anchor failed for %s: %s", asset, exc)
            live_price = 0.0
        if live_price > 0 and abs(live_price - entry) > 1e-9:
            shift = live_price / entry
            stop_loss = round(stop_loss * shift, 5) if stop_loss else stop_loss
            take_profit = (round(take_profit * shift, 5)
                           if take_profit else take_profit)
            reanchor_note = (f"re-anchor ราคาจริง {entry:g} → {live_price:g} "
                             f"(SL/TP ขยับตามสัดส่วน)")
            log.info("execute_signal re-anchor %s %s: %.5f → %.5f",
                     direction, asset, entry, live_price)
            entry = live_price

    # sl_distance_mode: stored signal rows always carry the กลาง (×1.5 ATR)
    # SL/TP (the default tier). If the user picked สั้น/ยาว in Settings,
    # re-derive SL/TP for the chosen tier from the entry and the base
    # (×1.5) distance so the REAL order matches the tier shown on the card.
    # Sizing also uses the re-derived SL so risk_per_trade_pct stays honest.
    if entry > 0 and stop_loss:
        base_dist = abs(entry - float(stop_loss))
        if base_dist > 0 and getattr(s, "sl_distance_mode", "medium") != "medium":
            sign = 1 if str(direction).upper() == "BUY" else -1
            tier = {"short": 1.0, "medium": 1.5, "long": 2.0}.get(
                s.sl_distance_mode, 1.5)
            dist = base_dist * (tier / 1.5)
            stop_loss = round(entry - sign * dist, 5)
            if take_profit:
                rr = abs(float(take_profit) - entry) / base_dist
                take_profit = round(entry + sign * dist * rr, 5)
            log.info("sl_distance_mode=%s → %s %s SL %.5f", s.sl_distance_mode,
                     direction, asset, stop_loss)
    report = _gate_blocked(db, s, user_id, asset, confidence, opportunity,
                           entry=entry, stop_loss=stop_loss)
    if not report.allowed:
        log.info("Execution blocked for %s %s: %s", direction, asset, report.rejects)
        # Lifecycle log: the gate said NO (pause/limits/news/correlation/...).
        signal_log.log_event(
            db=db, event="order_blocked", signal_id=str(signal_id or ""),
            asset=asset, direction=direction, confidence=confidence, entry=entry,
            source=source, reason="; ".join(report.rejects[:2]) or "gate blocked")
        return report

    lots = size_position(s, entry, stop_loss, asset=asset)
    if lots <= 0:
        report.allowed = False
        report.rejects.append("Position sizing returned 0 lots (bad entry/SL)")
        signal_log.log_event(
            db=db, event="order_blocked", signal_id=str(signal_id or ""),
            asset=asset, direction=direction, confidence=confidence, entry=entry,
            source=source, reason=report.rejects[-1])
        return report
    report.size_lots = lots

    # Paper realism: fill at entry ± spread/2. Spread resolves per symbol:
    # user override (spread_overrides) → built-in DEFAULT_SPREADS (realistic
    # typical spread, e.g. gold 0.30 vs EURUSD 0.00010) → legacy global
    # paper_spread. SL/TP stay anchored to the mid-based levels the signal
    # card showed; only the fill price moves, so the position starts with the
    # spread cost baked in exactly like a real account.
    fill_price = apply_spread(entry, direction, effective_spread(s, asset))

    result = await broker.place_order(OrderRequest(
        user_id=user_id, asset=asset, direction=direction, volume=lots,
        entry_price=fill_price, stop_loss=stop_loss, take_profit=take_profit,
    ))
    if not result.ok:
        report.allowed = False
        report.rejects.append(f"Broker rejected order: {result.message}")
        signal_log.log_event(
            db=db, event="order_blocked", signal_id=str(signal_id or ""),
            asset=asset, direction=direction, confidence=confidence, entry=entry,
            source=source, reason=report.rejects[-1])
        return report

    record_trade(db, {
        "user_id": user_id, "signal_id": signal_id, "asset": asset,
        "direction": direction, "volume": lots, "entry_price": fill_price,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "status": "open", "source": source, "ticket": result.broker_order_id,
    })
    # Lifecycle log: the order actually opened (ticket + volume recorded).
    signal_log.log_event(
        db=db, event="order_opened", signal_id=str(signal_id or ""),
        asset=asset, direction=direction, confidence=confidence, entry=entry,
        stop_loss=stop_loss, take_profit=take_profit, source=source,
        ticket=str(result.broker_order_id or ""), volume=lots,
        reason=f"เปิดออเดอร์ {direction} {lots:g} lots @ {fill_price:g} (" + (
            "auto" if source == "auto" else "อนุมัติเอง") + ")" + (
            f" — {reanchor_note}" if reanchor_note else ""))
    if notifier is not None:
        try:
            await notifier.notify(
                user_id, "trade_opened",
                f"🤖 {'AUTO' if source == 'auto' else 'APPROVED'} Trade Opened\n"
                f"Asset: {asset}\nDirection: {direction}\nVolume: {lots:.2f} lots"
                f"\nEntry: {fill_price:g}\nSL: {stop_loss if stop_loss is not None else '-'}"
                f"\nTP: {take_profit if take_profit is not None else '-'}"
                f"\nTicket: {result.broker_order_id}",
            )
        except Exception as exc:
            log.error("trade_opened notify failed: %s", exc)
    return report


# ---------------------------------------------------------------------------
# Monitor dashboard — one snapshot for the /monitor page
# ---------------------------------------------------------------------------
def _parse_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def monitor_snapshot(db, broker, s: AppSettings) -> "MonitorSnapshot":
    """Aggregate paper_trades + live broker marks + pause/kill state.

    All reads are fail-safe: a broken broker or missing rows degrade the
    dashboard, never raise.
    """
    from app.models.schemas import (
        MonitorOpenPosition, MonitorSnapshot, MonitorStats, MonitorTrade,
    )

    # ---- journal rows ----------------------------------------------------
    try:
        rows = db.select("paper_trades", limit=500)
    except Exception:
        rows = []
    open_rows = [r for r in rows if r.get("status") == "open"]
    closed_rows = [r for r in rows
                   if r.get("status") == "closed" and r.get("pnl") is not None]

    # ---- live marks ------------------------------------------------------
    # 1) broker-native marks for open tickets (fail-safe)
    marks: dict[str, float] = {}
    try:
        if hasattr(broker, "all_positions"):
            positions = broker.all_positions()
            if inspect.iscoroutine(positions):
                positions = await positions
            else:
                positions = list(positions)
        else:
            positions = []
        for pos in positions:
            price = getattr(pos, "current_price", None)
            if not price and hasattr(broker, "mark_price"):
                try:
                    price = broker.mark_price(pos.ticket)
                    if inspect.iscoroutine(price):
                        price = await price
                except Exception:
                    price = None
            if price:
                marks[str(pos.ticket)] = float(price)
                marks.setdefault("asset:" + str(getattr(pos, "asset", "")).upper(),
                                 float(price))
    except Exception as exc:
        log.warning("monitor: broker marks unavailable: %s", exc)

    # 2) live feed marks per asset (PaperBroker's internal book is a random
    # walk, NOT the market — the old code fell back to entry price which
    # pinned current_price == entry and showed PnL 0.00 forever).
    # Primary source is the intraday spot feed (Yahoo): Frankfurter publishes
    # only ONE close per business day, so intraday FX positions opened at
    # today's close would look "pinned" until tomorrow. Failures are NOT
    # silent: they land in feed_status so the UI can warn the user.
    feed_status: Optional["QuoteFeedStatus"] = None
    # Indicator snapshots fetched ONCE per cycle — the same dict tops up
    # live marks below AND feeds the Smart Exit block, so the monitor makes
    # exactly 1 spot batch + 1 snapshot batch (30s/60s caches keep both cheap).
    feed_snaps: dict[str, dict] = {}
    # Which assets got a live spot mark vs a daily-close fallback — the card
    # shows WHERE each price came from (spot / daily / broker / entry).
    spot_assets: set[str] = set()
    daily_assets: set[str] = set()
    if open_rows:
        from app.models.schemas import QuoteFeedStatus
        from app.integrations import quotes as quotes_mod
        assets = sorted({str(r["asset"]).upper() for r in open_rows
                         if r.get("asset")})
        try:
            prices, failures = await quotes_mod.fetch_spot_prices(assets)
            for asset, price in prices.items():
                if price > 0:
                    marks["asset:" + asset] = price
                    spot_assets.add(asset)
        except Exception as exc:  # whole-feed failure (shouldn't happen —
            # fetch_spot_prices isolates per-asset errors, but stay safe)
            prices, failures = {}, {a: str(exc) for a in assets}
            log.warning("monitor: spot feed unavailable: %s", exc)

        # Daily-close snapshots still top up assets the spot feed missed
        # (better than entry price, and works when Yahoo is down).
        if failures or not prices:
            try:
                feed_snaps = await quotes_mod.fetch_all_snapshots(
                    [a for a in assets if a not in prices])
                for asset, snap in feed_snaps.items():
                    price = float((snap or {}).get("price") or 0)
                    if price > 0:
                        marks["asset:" + asset] = price
                        daily_assets.add(asset)
            except Exception as exc:
                log.warning("monitor: daily-close fallback failed: %s", exc)
                feed_snaps = {}

        now_utc = datetime.now(timezone.utc)
        feed_status = QuoteFeedStatus(
            state="ok" if not failures else "error",
            source="exchangerate+yahoo",
            fetched_at=now_utc,
            failed_assets=sorted(failures),
            message="; ".join(failures[a] for a in sorted(failures))[:300],
        )

    def mark_for(row: dict) -> tuple[float, str]:
        """Resolve the best mark + WHERE it came from.

        Returns (price, source) with source in
        "spot" | "daily" | "broker" | "entry" so the UI can badge every
        mark instead of showing a bare number. Priority: live spot feed
        wins; daily-close snapshots top up what spot missed; broker book
        covers the rest; entry means "no feed, flat PnL".
        """
        ticket = str(row.get("ticket") or "")
        asset_u = str(row.get("asset") or "").upper()
        asset = "asset:" + asset_u
        if asset_u in spot_assets and asset in marks:
            return marks[asset], "spot"
        if asset_u in daily_assets and asset in marks:
            return marks[asset], "daily"
        if asset in marks:
            return marks[asset], "broker"
        if ticket and ticket in marks:
            return marks[ticket], "broker"
        return float(row.get("entry_price") or 0), "entry"  # unknown → flat, no guess

    # ---- Smart Exit analysis (per-position exit_info) --------------------
    # Read-only evaluation so the monitor shows the same score/quality/
    # recommendation the guard acts on. Fail-safe: disabled → None for all;
    # no indicator snapshot → None (blind HOLD, same rule as the guard).
    exit_snaps: dict[str, dict] = {}
    exit_news_status, exit_news_event = "SAFE", ""
    exit_avg_hold = 4.0
    exit_drawdown = 0.0
    smart_on = bool(getattr(s, "smart_exit_enabled", True))
    if smart_on and open_rows:
        # Reuse feed_snaps from the live-marks block above — only fetch the
        # assets the spot feed already covered (no second full batch).
        exit_snaps = dict(feed_snaps or {})
        try:
            from app.integrations import quotes as _quotes_exit
            _exit_assets = sorted({str(r["asset"]).upper() for r in open_rows
                                   if r.get("asset")})
            _missing = [a for a in _exit_assets if a not in exit_snaps]
            if _missing:
                _fresh = await _quotes_exit.fetch_all_snapshots(_missing)
                for _a, _snap in (_fresh or {}).items():
                    exit_snaps[_a] = _snap
        except Exception:
            pass
        try:
            _nr = _news_risk(db, s)
            exit_news_status = str(getattr(_nr, "status", "SAFE") or "SAFE").upper()
            _nxt = getattr(_nr, "next_high_impact", None)
            exit_news_event = str(getattr(_nxt, "event", "") or "") if _nxt else ""
        except Exception:
            exit_news_status, exit_news_event = "SAFE", ""
        exit_avg_hold = avg_hold_days(db, closed_rows)
        try:
            exit_drawdown = equity_drawdown_pct(db, float(getattr(s, "capital", 0) or 0))
        except Exception:
            exit_drawdown = 0.0

    def exit_info_for(row: dict, mark: float):
        """Build SmartExitInfo for one open row (None when unevaluated)."""
        if not smart_on:
            return None
        try:
            from app.engine import smart_exit as _se
            from app.models.schemas import SmartExitInfo as _SEInfo
            asset_u = str(row.get("asset") or "").upper()
            snap = dict(exit_snaps.get(asset_u) or {})
            if not snap:
                return None
            entry = float(row.get("entry_price") or 0)
            sl = float(row["stop_loss"]) if row.get("stop_loss") is not None else None
            created_dt = _parse_dt(row.get("created_at"))
            age = ((datetime.now(timezone.utc) - created_dt).total_seconds() / 86400.0
                   if created_dt else 0.0)
            age = max(0.0, age)
            d = _se.evaluate_exit(
                asset=asset_u,
                direction=str(row.get("direction") or "").upper(),
                entry_price=entry, price=float(mark),
                stop_loss=sl, age_days=age, settings=s,
                snapshot=snap, news_status=exit_news_status,
                news_event=exit_news_event,
                avg_hold_days=exit_avg_hold,
                drawdown_pct=exit_drawdown)
            return _SEInfo(
                position_age_days=d.position_age_days,
                r_multiple=d.r_multiple,
                exit_score=d.exit_score, quality=d.quality,
                factors={
                    "trend_strength": d.factors.trend_strength,
                    "momentum": d.factors.momentum,
                    "volume_proxy": d.factors.volume_proxy,
                    "market_regime": d.factors.market_regime,
                    "news_risk": d.factors.news_risk,
                    "holding_time": d.factors.holding_time,
                    "volatility": d.factors.volatility,
                    "opportunity_score": d.factors.opportunity_score,
                    "risk_exposure": d.factors.risk_exposure,
                },
                signals={
                    "tp_hit": d.signals.tp_hit, "sl_hit": d.signals.sl_hit,
                    "trailing": d.signals.trailing,
                    "reversal": d.signals.reversal,
                    "news": d.signals.news,
                    "time_stop": d.signals.time_stop,
                },
                recommendation=d.recommendation, final=d.final,
                reasoning=list(d.reasoning or []), trigger=d.trigger,
            )
        except Exception:
            return None

    open_positions = []
    for r in open_rows:
        mark, price_source = mark_for(r)
        entry = float(r.get("entry_price") or 0)
        # asset ต้องส่งเข้าไปด้วย — ไม่งั้น PaperBrokerPnl ใช้ FX contract
        # 100,000 กับ XAUUSD (ควรเป็น 100 oz) → uPnL ผิด 1,000 เท่า
        unrealized = round(PaperBrokerPnl.compute(SimpleNamespace(
            direction=str(r["direction"]).upper(),
            current_price=mark,
            entry_price=entry,
            volume=float(r.get("volume") or 0),
            asset=str(r.get("asset") or ""))), 2)
        # --- Explainability: R / $risk / source / step-by-step notes ---
        # Every number on the monitor table gets its derivation so the UI
        # shows math instead of bare values. Fail-safe: any error → zeros.
        r_multiple = 0.0
        risk_amount = 0.0
        calc_notes: list[str] = []
        exit_info = exit_info_for(r, mark)
        try:
            asset_u = str(r.get("asset") or "").upper()
            direction_u = str(r.get("direction") or "").upper()
            sign = 1.0 if direction_u == "BUY" else -1.0
            vol = float(r.get("volume") or 0)
            sl_v = float(r["stop_loss"]) if r.get("stop_loss") is not None else None
            tp_v = float(r["take_profit"]) if r.get("take_profit") is not None else None
            contract = contract_value_for(asset_u)
            sl_dist = abs(entry - sl_v) if (sl_v is not None and entry) else 0.0
            if sl_dist > 0:
                r_multiple = round(sign * (mark - entry) / sl_dist, 2)
                risk_amount = round(sl_dist * vol * contract, 2)
            src_label = {"spot": "spot สด (Yahoo intraday)",
                         "daily": "daily close (สำรองตอน spot ล่ม)",
                         "broker": "broker book (สำรอง)",
                         "entry": "entry (ไม่มี feed — PnL นิ่ง)"}.get(
                             price_source, price_source or "?")
            calc_notes.append(
                f"ราคาปัจจุบัน {mark:g} มาจาก {src_label}")
            calc_notes.append(
                f"uPnL = {'+' if sign > 0 else '−'}(mark−entry) × {vol:g} lots "
                f"× contract {contract:g} → ${unrealized:,.2f}")
            if sl_dist > 0:
                calc_notes.append(
                    f"R = {direction_u}×({mark:g}−{entry:g}) ÷ SLระยะ {sl_dist:g} "
                    f"→ {r_multiple:+.2f}R")
                calc_notes.append(
                    f"ถ้าโดน SL เสีย ${risk_amount:,.2f} "
                    f"({sl_dist:g} × {vol:g} × {contract:g})")
            if tp_v is not None and sl_dist > 0:
                tp_r = abs(tp_v - entry) / sl_dist
                calc_notes.append(f"TP อยู่ที่ {tp_r:.2f}R (TP {tp_v:g})")
            be_r = float(getattr(s, "breakeven_trigger_r", 0) or 0)
            if be_r > 0 and sl_dist > 0:
                be_price = entry + sign * be_r * sl_dist
                calc_notes.append(
                    f"Breakeven ที่ +{be_r:g}R → ราคา {be_price:g} "
                    f"(ถึงแล้ว SL ย้ายมาทุน)")
            pt_r = float(getattr(s, "partial_trigger_r", 0) or 0)
            pt_pct = float(getattr(s, "partial_close_pct", 0) or 0)
            if pt_r > 0 and pt_pct > 0 and sl_dist > 0:
                pt_price = entry + sign * pt_r * sl_dist
                calc_notes.append(
                    f"แบ่งปิด {pt_pct:g}% ที่ +{pt_r:g}R → ราคา {pt_price:g}")
            isl = r.get("initial_stop_loss")
            if (isl is not None and sl_v is not None
                    and abs(float(isl) - sl_v) > 1e-9):
                calc_notes.append(
                    f"SL ขยับ {float(isl):g} → {sl_v:g} "
                    f"({str(r.get('sl_move_reason') or '') or 'guard'})")
            if exit_info is not None:
                calc_notes.append(
                    f"Smart Exit {exit_info.exit_score:.0f}/100 "
                    f"({exit_info.quality}) → {exit_info.final}")
        except Exception:
            pass
        open_positions.append(MonitorOpenPosition(
            id=str(r.get("id")), ticket=str(r.get("ticket") or ""),
            asset=r["asset"], direction=str(r["direction"]).upper(),
            volume=float(r.get("volume") or 0),
            entry_price=entry,
            stop_loss=float(r["stop_loss"]) if r.get("stop_loss") is not None else None,
            take_profit=float(r["take_profit"]) if r.get("take_profit") is not None else None,
            current_price=mark,
            unrealized_pnl=unrealized,
            source=r.get("source", "auto"),
            created_at=_parse_dt(r.get("created_at")),
            # SL/TP move tracking (migration 021) — the UI badges cells whose
            # value differs from the level the position opened with.
            initial_stop_loss=float(r["initial_stop_loss"]) if r.get("initial_stop_loss") is not None else None,
            initial_take_profit=float(r["initial_take_profit"]) if r.get("initial_take_profit") is not None else None,
            sl_moved_at=_parse_dt(r.get("sl_moved_at")),
            sl_move_reason=str(r.get("sl_move_reason") or ""),
            tp_moved_at=_parse_dt(r.get("tp_moved_at")),
            tp_move_reason=str(r.get("tp_move_reason") or ""),
            exit_info=exit_info,
            r_multiple=r_multiple,
            risk_amount=risk_amount,
            price_source=price_source,
            calc_notes=calc_notes,
        ))

    recent = [MonitorTrade(
        id=str(r.get("id")), asset=r["asset"],
        direction=str(r["direction"]).upper(),
        volume=float(r.get("volume") or 0),
        entry_price=float(r.get("entry_price") or 0),
        exit_price=float(r["exit_price"]) if r.get("exit_price") is not None else None,
        pnl=float(r["pnl"]) if r.get("pnl") is not None else None,
        status=r.get("status", "open"), source=r.get("source", "auto"),
        ticket=r.get("ticket"), close_reason=r.get("close_reason"),
        closed_at=_parse_dt(r.get("closed_at")),
        created_at=_parse_dt(r.get("created_at")),
    ) for r in sorted(
        rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)[:50]]

    # ---- stats -----------------------------------------------------------
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_ago = now - timedelta(days=7)

    def created(r: dict) -> Optional[datetime]:
        return _parse_dt(r.get("created_at"))

    today_rows = [r for r in rows if (created(r) and created(r).date().isoformat() == today)]
    week_rows = [r for r in rows if (created(r) and created(r) >= week_ago)
                 and r.get("status") != "rejected"]
    wins = [r for r in closed_rows if float(r.get("pnl") or 0) > 0]
    stats = MonitorStats(
        trades_today=len(today_rows),
        trades_week=len(week_rows),
        open_positions=len(open_rows),
        closed_count=len(closed_rows),
        win_rate=round(len(wins) / len(closed_rows) * 100, 1) if closed_rows else 0.0,
        pnl_today=round(sum(float(r.get("pnl") or 0) for r in today_rows), 2),
        pnl_week=round(sum(float(r.get("pnl") or 0) for r in week_rows), 2),
        pnl_total=round(sum(float(r.get("pnl") or 0) for r in closed_rows), 2),
    )

    # ---- live portfolio value (home page Current Equity / Current PnL) ----
    # PnL = realized (closed rows) + unrealized (live marks) — computed from
    # the DB so every page shares one truth and stats-reset zeroes it.
    unrealized_total = round(sum(p.unrealized_pnl for p in open_positions), 2)
    live_pnl = round(stats.pnl_total + unrealized_total, 2)
    live_equity = round(s.capital + live_pnl, 2)

    # ---- kill switch (single shared path — see evaluate_kill) ------------
    kill = evaluate_kill(db, s)

    # ---- real Risk Engine status (plan A 2026-09-09) ----------------------
    # Same inputs the portfolio_monitor worker uses: open risk in account
    # currency (|entry-SL| x lots x contract), live equity, realized PnL
    # windows and snapshot peak — so the monitor card can never disagree
    # with the worker's pause verdict again. Fail-safe: any error degrades
    # to None (the card shows "no data", never a fake "low").
    risk_status = None
    try:
        from app.engine.risk_engine import (
            PortfolioSnapshot as _RiskSnap,
            risk_engine_for_settings as _engine_for,
        )
        _open_risk = 0.0
        for _t in open_rows:
            if _t.get("stop_loss") and _t.get("entry_price"):
                try:
                    _contract = PaperBrokerPnl.CONTRACT_SIZES.get(
                        str(_t.get("asset") or "").upper(), 100_000.0)
                except Exception:
                    _contract = 100_000.0
                _open_risk += abs(float(_t["entry_price"]) - float(_t["stop_loss"])) \
                    * float(_t.get("volume") or 1) * _contract
        _daily, _weekly, _monthly = _loss_pcts(db, float(s.capital or 0))
        _risk_snap = _RiskSnap(
            starting_capital=float(s.capital or 0),
            peak_equity=peak_equity(db, float(s.capital or 0), live_equity),
            current_equity=live_equity,
            realized_pnl_today=-_daily / 100.0 * float(s.capital or 0),
            realized_pnl_week=-_weekly / 100.0 * float(s.capital or 0),
            realized_pnl_month=-_monthly / 100.0 * float(s.capital or 0),
            open_risk=_open_risk,
            open_positions=len(open_rows),
        )
        risk_status = _engine_for(s).check(_risk_snap)
    except Exception as exc:
        log.warning("monitor: risk eval failed: %s", exc)
        risk_status = None

    return MonitorSnapshot(
        pause=get_pause(db), order_mode=s.order_mode, capital=s.capital,
        kill=kill, risk=risk_status, stats=stats, open_positions=open_positions, recent=recent,
        generated_at=now,
        feed_status=feed_status,
        equity=live_equity, pnl=live_pnl,
    )
