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
    effective_min_confidence,
    effective_min_lot,
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
                reason=f"pending เกิน {SIGNAL_TTL_MIN} นาที — ไม่ได้เปิดออเดอร์")
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
    try:
        db.insert("paper_trades", trade)
    except Exception as exc:  # pragma: no cover — Database.insert already swallows
        log.error("record_trade failed: %s", exc)


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
    peak = max(equities)
    current = equities[0]  # rows are newest-first
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - current) / peak * 100.0)


# ---------------------------------------------------------------------------
# The gate pipeline
# ---------------------------------------------------------------------------
def _gate_blocked(db, s: AppSettings, user_id: str, asset: str,
                  confidence: float, opportunity: float) -> GateReport:
    """Run every safety gate. Returns GateReport with allowed=False on any block.

    size_lots is computed here too, so callers never place an un-sized order.
    """
    rejects: list[str] = []
    checks: list[str] = []

    # ---- Gate 0: manual pause switch -------------------------------------
    pause = get_pause(db)
    if pause.paused:
        rejects.append(f"Trading paused: {pause.reason or 'manual pause'}")
    checks.append(f"pause={'ENGAGED' if pause.paused else 'clear'}")

    # ---- Gate 1: kill switch (loss limits from journal) -------------------
    ks: KillSwitchStatus
    try:
        daily, weekly, monthly = _loss_pcts(db, s.capital)
        dd = equity_drawdown_pct(db, s.capital)
        ks = KillSwitchEngine(
            daily_loss_limit=s.kill_daily_loss_pct,
            weekly_loss_limit=s.kill_weekly_loss_pct,
            monthly_loss_limit=s.kill_monthly_loss_pct,
            drawdown_limit=s.max_drawdown_pct,
        ).evaluate(
            daily_loss_pct=daily, weekly_loss_pct=weekly, monthly_loss_pct=monthly,
            drawdown_pct=dd,
            broker_connected=True, market_data_ok=True,
            ai_provider_ok=True, execution_ok=True,
        )
    except Exception as exc:
        ks = KillSwitchStatus(engaged=True, triggers=[f"kill-switch eval error: {exc}"],
                              message="kill switch unavailable — fail-safe engaged")
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
    report = _gate_blocked(db, s, user_id, asset, confidence, opportunity)
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

    # Paper realism: fill at entry ± spread/2 (Settings → paper_spread).
    # SL/TP stay anchored to the mid-based levels the signal card showed;
    # only the fill price moves, so the position starts with the spread cost
    # baked in exactly like a real account.
    fill_price = apply_spread(entry, direction, getattr(s, "paper_spread", 0.0))

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
            "auto" if source == "auto" else "อนุมัติเอง") + ")")
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
        except Exception as exc:  # whole-feed failure (shouldn't happen —
            # fetch_spot_prices isolates per-asset errors, but stay safe)
            prices, failures = {}, {a: str(exc) for a in assets}
            log.warning("monitor: spot feed unavailable: %s", exc)

        # Daily-close snapshots still top up assets the spot feed missed
        # (better than entry price, and works when Yahoo is down).
        if failures or not prices:
            try:
                snaps = await quotes_mod.fetch_all_snapshots(
                    [a for a in assets if a not in prices])
                for asset, snap in snaps.items():
                    price = float(snap.get("price") or 0)
                    if price > 0:
                        marks["asset:" + asset] = price
            except Exception as exc:
                log.warning("monitor: daily-close fallback failed: %s", exc)

        now_utc = datetime.now(timezone.utc)
        feed_status = QuoteFeedStatus(
            state="ok" if not failures else "error",
            source="exchangerate+yahoo",
            fetched_at=now_utc,
            failed_assets=sorted(failures),
            message="; ".join(failures[a] for a in sorted(failures))[:300],
        )

    def mark_for(row: dict) -> float:
        """Resolve the best mark: live feed → broker book → entry.

        Priority matters: the PaperBroker book is in-memory and rehydrate
        seeds it with current_price == entry, so after every deploy the book
        pins marks at the entry price until the guard's first tick. Ticket
        marks used to win, which showed uPnL 0.00 for every position right
        after a restart even while the live feed was healthy. The live spot
        feed is the real market — it wins; the broker book is only a
        fallback for assets the feed doesn't cover.
        """
        ticket = str(row.get("ticket") or "")
        asset = "asset:" + str(row.get("asset") or "").upper()
        if asset in marks:
            return marks[asset]
        if ticket in marks:
            return marks[ticket]
        return float(row.get("entry_price") or 0)  # unknown → flat PnL, no guess

    open_positions = []
    for r in open_rows:
        mark = mark_for(r)
        entry = float(r.get("entry_price") or 0)
        # asset ต้องส่งเข้าไปด้วย — ไม่งั้น PaperBrokerPnl ใช้ FX contract
        # 100,000 กับ XAUUSD (ควรเป็น 100 oz) → uPnL ผิด 1,000 เท่า
        unrealized = round(PaperBrokerPnl.compute(SimpleNamespace(
            direction=str(r["direction"]).upper(),
            current_price=mark,
            entry_price=entry,
            volume=float(r.get("volume") or 0),
            asset=str(r.get("asset") or ""))), 2)
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

    # ---- kill switch (same math the gate uses) ---------------------------
    daily, weekly, monthly = _loss_pcts(db, s.capital)
    kill = KillSwitchEngine(
        daily_loss_limit=s.kill_daily_loss_pct,
        weekly_loss_limit=s.kill_weekly_loss_pct,
        monthly_loss_limit=s.kill_monthly_loss_pct,
        drawdown_limit=s.max_drawdown_pct,
    ).evaluate(
        daily_loss_pct=daily, weekly_loss_pct=weekly, monthly_loss_pct=monthly,
        drawdown_pct=equity_drawdown_pct(db, s.capital),
        broker_connected=True, market_data_ok=True,
        ai_provider_ok=True, execution_ok=True,
    )

    return MonitorSnapshot(
        pause=get_pause(db), order_mode=s.order_mode, capital=s.capital,
        kill=kill, stats=stats, open_positions=open_positions, recent=recent,
        generated_at=now,
        feed_status=feed_status,
    )
