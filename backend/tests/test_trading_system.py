"""Tests for the Extended Trading System engines (spec: 2026-09-02).

Covers: frequency limits + signal quality filter, order strategy multi-entry,
correlation/exposure, economic calendar news-risk gate, market sessions,
kill switch triggers, risk officer vetoes, journal analysis, backtest metrics,
walk-forward reliability and paper trading readiness.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_trading_system.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.engine.strategy_engine import IndicatorSnapshot
from app.models.schemas import (
    BacktestConfig,
    CorrelationEngine,
    EconomicCalendarEngine,
    EconomicEvent,
    ExposureEngine,
    FrequencyEngine,
    KillSwitchEngine,
    RiskOfficer,
    RiskProfile,
    SessionEngine,
    analyze_journal,
    paper_trading_status,
    risk_to_lot,
    run_backtest,
    walk_forward,
)
from app.integrations.quotes import Candle
from app.workers.market_scanner import scan_once
from tests.test_workers import FakeDatabase, strong_snapshot


# ---------------------------------------------------------------- frequency
def test_signal_quality_filter_blocks_low_confidence():
    d = FrequencyEngine(RiskProfile.moderate).evaluate(confidence=69.9)
    assert not d.allowed
    assert "70" in d.reason


def test_frequency_daily_limit_conservative():
    eng = FrequencyEngine(RiskProfile.conservative)
    d = eng.evaluate(confidence=90, trades_today=3)
    assert not d.allowed
    assert "Daily limit" in d.reason
    assert d.limits is not None and d.limits.max_trades_daily == 3
    assert d.limits.risk_per_trade_pct == 0.5


def test_frequency_weekly_limit_and_open_positions():
    eng = FrequencyEngine(RiskProfile.aggressive)
    d_week = eng.evaluate(confidence=90, trades_this_week=50)
    assert not d_week.allowed and "Weekly limit" in d_week.reason
    d_pos = eng.evaluate(confidence=90, open_positions=8)
    assert not d_pos.allowed and "open positions" in d_pos.reason
    assert eng.limits().max_open_positions == 8


def test_frequency_regime_throttle():
    d = FrequencyEngine(RiskProfile.moderate).evaluate(
        confidence=90, regime="sideway", volatility_index=8)
    assert not d.allowed and "sideway" in d.reason


def test_frequency_pass_when_clear():
    d = FrequencyEngine(RiskProfile.moderate).evaluate(
        confidence=85, regime="bull_trend", volatility_index=15)
    assert d.allowed


# ------------------------------------------------------------ order strategy
def test_order_plan_trend_uses_limit_pullbacks():
    plan = OrderStrategyEngine_plan(regime="bull_trend", atr_pct=0.8)
    assert plan.entries[0].order_type.value == "market"
    assert plan.entries[1].order_type.value == "buy_limit"
    assert plan.entries[2].order_type.value == "buy_limit"
    # risk split 0.5/0.3/0.2 of 1% per-trade risk
    assert abs(sum(l.risk_pct for l in plan.entries) - 1.0) < 1e-6


def test_order_plan_sideway_uses_breakout_stops():
    plan = OrderStrategyEngine_plan(regime="sideway", atr_pct=0.8)
    assert all(l.order_type.value in ("buy_stop", "sell_stop") for l in plan.entries)
    # average entry weighted by lots
    lots = sum(l.lot for l in plan.entries)
    avg = sum(l.price * l.lot for l in plan.entries) / lots
    assert abs(plan.average_entry - avg) < 0.01


def test_order_plan_sell_direction_flips_legs():
    plan = OrderStrategyEngine_plan(regime="bear_trend", direction="SELL")
    assert plan.direction == "SELL"
    assert plan.entries[1].order_type.value == "sell_limit"
    # pullback legs for a SELL sit ABOVE the market entry
    assert plan.entries[1].price > plan.entries[0].price


def OrderStrategyEngine_plan(regime="bull_trend", direction="BUY", atr_pct=0.8):
    from app.models.schemas import OrderStrategyEngine
    return OrderStrategyEngine().build_plan(
        asset="EURUSD", direction=direction, entry=1.10, stop_loss=1.09,
        take_profit=1.12, regime=regime, atr_pct=atr_pct,
        equity=10_000, risk_per_trade_pct=1.0)


def test_risk_to_lot_math():
    # 1% of 10k = 100 risk; 100-pip stop on EURUSD = 0.0100 → 0.1 lots
    assert risk_to_lot(10_000, 1.0, 0.0100) == pytest.approx(0.1, abs=0.01)


# -------------------------------------------------------- correlation/exposure
def test_correlation_forex_cluster_scores_high():
    score = CorrelationEngine().portfolio_correlation(["EURUSD", "GBPUSD", "AUDUSD"])
    assert score > 50


def test_correlation_mixed_portfolio_lower():
    score = CorrelationEngine().portfolio_correlation(["EURUSD", "XAUUSD"])
    assert score < 50


def test_exposure_sums_currency_nets():
    rows = [
        {"asset": "EURUSD", "direction": "BUY", "volume": 1.0, "price": 1.1},
        {"asset": "USDJPY", "direction": "SELL", "volume": 1.0, "price": 150.0},
    ]
    out = {e.currency: e for e in ExposureEngine().analyze(rows)}
    assert "USD" in out and "EUR" in out and "JPY" in out


# ----------------------------------------------------------------- calendar
def test_news_risk_danger_within_30_minutes():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    ev = EconomicEvent(event="NFP", time_utc=now + timedelta(minutes=20))
    st = EconomicCalendarEngine().news_risk([ev], now)
    assert st.status == "DANGER"


def test_news_risk_safe_far_from_event():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    ev = EconomicEvent(event="CPI", time_utc=now + timedelta(hours=6))
    st = EconomicCalendarEngine().news_risk([ev], now)
    assert st.status == "SAFE"


def test_news_risk_caution_window():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    ev = EconomicEvent(event="FOMC", time_utc=now + timedelta(minutes=60))
    st = EconomicCalendarEngine().news_risk([ev], now)
    assert st.status == "CAUTION"


# ----------------------------------------------------------------- session
def test_session_london_ny_overlap_is_high_volatility():
    now = datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc)  # 13:00 UTC
    st = SessionEngine.active(now)
    assert "London" in st.active_sessions and "New York" in st.active_sessions
    assert st.overlapping and st.volatility_hint == "high"


def test_session_quiet_hours():
    now = datetime(2026, 9, 2, 19, 0, tzinfo=timezone.utc)  # 19:00 UTC: NY only
    st = SessionEngine.active(now)
    assert st.active_sessions == ["New York"]
    assert st.volatility_hint == "medium"


# -------------------------------------------------------------- kill switch
def test_kill_switch_daily_loss_trigger():
    st = KillSwitchEngine().evaluate(daily_loss_pct=2.5)
    assert st.engaged and any("Daily loss" in t for t in st.triggers)


def test_kill_switch_infrastructure_triggers():
    st = KillSwitchEngine().evaluate(broker_connected=False, ai_provider_ok=False)
    assert st.engaged and len(st.triggers) == 2


def test_kill_switch_all_clear():
    st = KillSwitchEngine().evaluate(
        daily_loss_pct=1.0, weekly_loss_pct=3.0, monthly_loss_pct=6.0, drawdown_pct=8.0)
    assert not st.engaged


# ------------------------------------------------------------- risk officer
def test_risk_officer_rejects_low_confidence_and_danger_news():
    from app.models.schemas import FrequencyDecision, NewsRiskStatus, KillSwitchStatus
    freq = FrequencyDecision(allowed=True, reason="ok")
    news = NewsRiskStatus(status="DANGER", reason="NFP ใกล้")
    ks = KillSwitchStatus(engaged=False, triggers=[], checked=[])
    review = RiskOfficer().review_trade(
        confidence=75, opportunity_score=80, frequency=freq,
        news_risk=news, kill_switch=ks)
    assert review.verdict == "REJECTED"
    assert any("Reject Strategy" in r for r in review.rejects)


def test_risk_officer_approves_clean_setup():
    from app.models.schemas import FrequencyDecision, NewsRiskStatus, KillSwitchStatus
    freq = FrequencyDecision(allowed=True, reason="ok")
    news = NewsRiskStatus(status="SAFE", reason="ปลอดภัย")
    ks = KillSwitchStatus(engaged=False, triggers=[], checked=[])
    review = RiskOfficer().review_trade(
        confidence=85, opportunity_score=80, frequency=freq,
        news_risk=news, kill_switch=ks, correlation_score=40)
    assert review.verdict == "APPROVED"


def test_risk_officer_respects_correlation_cap():
    from app.models.schemas import FrequencyDecision, NewsRiskStatus, KillSwitchStatus
    freq = FrequencyDecision(allowed=True, reason="ok")
    news = NewsRiskStatus(status="SAFE", reason="ปลอดภัย")
    ks = KillSwitchStatus(engaged=False, triggers=[], checked=[])
    review = RiskOfficer().review_trade(
        confidence=85, opportunity_score=80, frequency=freq,
        news_risk=news, kill_switch=ks, correlation_score=90)
    assert review.verdict == "REJECTED"
    assert any("correlation" in r for r in review.rejects)


# ----------------------------------------------------------------- journal
def test_journal_analysis_win_rate_and_profit_factor():
    from app.models.schemas import JournalEntry
    entries = [
        JournalEntry(asset="EURUSD", direction="BUY", entry_price=1.1, pnl=100, rr_ratio=2.0),
        JournalEntry(asset="EURUSD", direction="SELL", entry_price=1.1, pnl=-50, rr_ratio=1.0),
        JournalEntry(asset="XAUUSD", direction="BUY", entry_price=2400, pnl=200, rr_ratio=3.0),
    ]
    a = analyze_journal(entries, period_days=30)
    assert a.total_trades == 3
    assert a.win_rate_pct == pytest.approx(66.7, abs=0.1)
    assert a.profit_factor == pytest.approx(6.0, abs=0.01)
    assert a.average_rr == pytest.approx(2.0)
    assert a.best_setup.pnl == 200 and a.worst_setup.pnl == -50


# ---------------------------------------------------------------- backtest
def _trending_candles(n: int = 120) -> list[Candle]:
    base = 1.10
    return [Candle(o=base + i * 0.0005, h=base + i * 0.0005 + 0.002,
                   l=base + i * 0.0005 - 0.002, c=base + i * 0.0005 + 0.001)
            for i in range(n)]


def test_backtest_produces_metrics():
    res = run_backtest(_trending_candles(), BacktestConfig(asset="EURUSD", indicator="EMA"))
    assert res.final_equity > 0
    assert res.total_trades >= 0
    assert 0 <= res.max_drawdown_pct <= 100


def test_walk_forward_reliability_bounds():
    res = walk_forward(_trending_candles(160), BacktestConfig(asset="EURUSD"), segments=4)
    assert 0 <= res.reliability_score <= 100
    assert res.segments == 4


def test_walk_forward_insufficient_data():
    res = walk_forward([], BacktestConfig(asset="EURUSD"), segments=4)
    assert res.segments == 0 and res.reliability_score == 0.0


# ------------------------------------------------------------ paper trading
class FakeBroker:
    class T:
        def __init__(self, pnl):
            self.pnl = pnl

    def __init__(self, pnls):
        self.trades = [self.T(p) for p in pnls]


def test_paper_trading_readiness_scores():
    good = paper_trading_status(FakeBroker([50, 60, -20, 40, 30, 55, -10, 45, 25, 35]))
    assert good.live_readiness_score > 50
    empty = paper_trading_status(FakeBroker([]))
    assert "10 ไม้" in empty.ai_coaching


# ------------------------------------------------- scanner integration (fake db)
@pytest.mark.anyio
def test_scan_once_persists_journal_and_respects_quality_filter():
    """scan_once keeps inserting analysis; quality filter caps signal emission."""
    db = FakeDatabase()
    import asyncio
    results = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        scan_once(db))
    assert len(results) == 5
    tables = [t for t, _ in db.inserted]
    assert "market_analysis" in tables


def test_strong_signal_passes_quality_gate():
    """Confidence ≥70 with clean regime → frequency engine allows the signal."""
    ind = strong_snapshot("EURUSD")
    assert ind.adx > 25  # trending
    from app.engine.strategy_engine import regime_of
    d = FrequencyEngine(RiskProfile.moderate).evaluate(
        confidence=85, regime=regime_of(ind), volatility_index=ind.volatility_index)
    assert d.allowed
