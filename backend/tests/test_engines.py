"""Unit tests for the pure engines — no DB, no network, no external deps.

Run from backend/:
    C:/Python314/python.exe -m pytest tests/ -v
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Goal Engine
# ---------------------------------------------------------------------------
from app.engine.goal_engine import PROFILE_ENVELOPES, GoalEngine
from app.models.schemas import GoalInput, Probability, RiskProfile

from datetime import datetime, timezone


def make_goal(capital=100_000, target=3.0, profile="moderate", max_dd=10.0):
    return GoalInput(
        capital=capital,
        target_return_pct=target,
        risk_profile=RiskProfile(profile),
        max_drawdown_pct=max_dd,
        trading_mode="manual",
    )


class TestGoalEngine:
    def test_expected_profit_basic(self):
        assert GoalEngine.expected_profit(100_000, 3.0) == 3000.0

    def test_expected_profit_zero_target(self):
        assert GoalEngine.expected_profit(50_000, 0.0) == 0.0

    def test_assess_high_probability_when_target_in_envelope(self):
        result = GoalEngine().assess(make_goal(target=2.0))
        assert result.probability == Probability.high
        assert result.risk_warning is None

    def test_assess_moderate_probability_when_target_stretched(self):
        # moderate hi=3.0, 1.75x = 5.25 → target 5.0 → moderate
        result = GoalEngine().assess(make_goal(target=5.0))
        assert result.probability == Probability.moderate
        assert result.risk_warning is not None

    def test_assess_low_probability_when_target_unrealistic(self):
        result = GoalEngine().assess(make_goal(target=20.0))
        assert result.probability == Probability.low
        assert result.risk_warning is not None

    def test_scenarios_best_normal_worst_ordering(self):
        result = GoalEngine().assess(make_goal())
        labels = [s.label for s in result.scenarios]
        assert labels == ["best_case", "normal_case", "worst_case"]
        best, normal, worst = result.scenarios
        assert best.expected_return_pct > normal.expected_return_pct > worst.expected_return_pct

    def test_scenario_profits_consistent_with_pct(self):
        result = GoalEngine().assess(make_goal(capital=200_000))
        for s in result.scenarios:
            assert s.expected_profit == round(200_000 * s.expected_return_pct / 100.0, 2)

    def test_all_profiles_have_envelopes(self):
        for p in ("conservative", "moderate", "aggressive"):
            assert p in PROFILE_ENVELOPES

    def test_conservative_best_case_still_bounded(self):
        result = GoalEngine().assess(make_goal(profile="conservative", target=1.0))
        best = result.scenarios[0]
        assert best.expected_return_pct == PROFILE_ENVELOPES["conservative"][2]  # 3.0

    def test_reasoning_is_nonempty(self):
        result = GoalEngine().assess(make_goal())
        assert isinstance(result.reasoning, list)
        assert len(result.reasoning) >= 3
        assert all(len(r) > 10 for r in result.reasoning)


# ---------------------------------------------------------------------------
# Strategy Engine — opportunity scoring
# ---------------------------------------------------------------------------
from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
from app.models.schemas import FinalDecision, OpportunityBand


def make_ind(**kw) -> IndicatorSnapshot:
    """A strongly bullish trending snapshot by default."""
    base = dict(
        asset="XAUUSD",
        price=2400.0,
        ema_fast=2420.0,
        ema_slow=2380.0,
        adx=32.0,
        supertrend_dir=1,
        rsi=60.0,
        macd_hist=2.5,
        atr_pct=0.9,
        volatility_index=15.0,
        news_sentiment=0.5,
        high_impact_event=False,
    )
    base.update(kw)
    return IndicatorSnapshot(**base)


class TestOpportunityScore:
    def setup_method(self):
        self.engine = StrategyEngine()

    def test_perfect_bullish_scores_high(self):
        opp = self.engine.opportunity_score(make_ind())
        assert opp.score >= 70
        assert opp.band in (OpportunityBand.high, OpportunityBand.very_high)

    def test_score_bounded_0_to_100(self):
        for kwargs in (
            dict(ema_fast=0, ema_slow=0, adx=0, rsi=50, macd_hist=0, atr_pct=0, news_sentiment=0),
            dict(ema_fast=1, ema_slow=0, adx=50, rsi=65, macd_hist=5, atr_pct=1.0, news_sentiment=1),
            dict(ema_fast=0, ema_slow=5, adx=10, rsi=10, macd_hist=-5, atr_pct=5.0, news_sentiment=-1, high_impact_event=True),
        ):
            opp = self.engine.opportunity_score(make_ind(**kwargs))
            assert 0.0 <= opp.score <= 100.0

    def test_bearish_scores_lower_than_bullish(self):
        bull = self.engine.opportunity_score(make_ind()).score
        bear = self.engine.opportunity_score(
            make_ind(ema_fast=2380.0, ema_slow=2420.0, supertrend_dir=-1, rsi=40, macd_hist=-2.0)
        ).score
        assert bear < bull

    def test_high_impact_event_penalizes(self):
        calm = self.engine.opportunity_score(make_ind()).score
        shaky = self.engine.opportunity_score(make_ind(high_impact_event=True)).score
        assert shaky < calm

    def test_extreme_volatility_penalized(self):
        calm = self.engine.opportunity_score(make_ind(atr_pct=0.9)).score
        wild = self.engine.opportunity_score(make_ind(atr_pct=3.5)).score
        assert wild < calm

    def test_band_boundaries(self):
        f = StrategyEngine.band_of
        assert f(0.0) == OpportunityBand.low
        assert f(30.9) == OpportunityBand.low
        assert f(31.0) == OpportunityBand.medium
        assert f(60.9) == OpportunityBand.medium
        assert f(61.0) == OpportunityBand.high
        assert f(80.9) == OpportunityBand.high
        assert f(81.0) == OpportunityBand.very_high
        assert f(100.0) == OpportunityBand.very_high

    def test_reasons_are_populated(self):
        opp = self.engine.opportunity_score(make_ind())
        assert len(opp.reasons) >= 3


class TestBuildProposal:
    def setup_method(self):
        self.engine = StrategyEngine()

    def test_proposal_buy_in_bull_regime(self):
        ind = make_ind()
        opp = self.engine.opportunity_score(ind)
        p = self.engine.build_proposal(ind, opp, risk_per_trade_pct=0.5, regime_bullish=True)
        assert p.direction == "BUY"
        assert p.stop_loss < p.entry < p.take_profit  # long: SL below entry, TP above

    def test_proposal_sell_in_bear_regime(self):
        ind = make_ind()
        opp = self.engine.opportunity_score(ind)
        p = self.engine.build_proposal(ind, opp, risk_per_trade_pct=0.5, regime_bullish=False)
        assert p.direction == "SELL"
        assert p.take_profit < p.entry < p.stop_loss  # short: mirrored

    def test_sl_is_atr_multiple(self):
        ind = make_ind(price=2400.0, atr_pct=1.0)
        opp = self.engine.opportunity_score(ind)
        p = self.engine.build_proposal(ind, opp, risk_per_trade_pct=0.5, regime_bullish=True, atr_multiple_sl=1.5)
        assert abs((p.entry - p.stop_loss) - 36.0) < 0.01  # 2400 * 1% * 1.5

    def test_rr_multiple_maintained(self):
        ind = make_ind()
        opp = self.engine.opportunity_score(ind)
        p = self.engine.build_proposal(ind, opp, 0.5, True, rr_target=2.0)
        sl = p.entry - p.stop_loss
        tp = p.take_profit - p.entry
        assert abs(tp - sl * 2.0) < 0.05

    def test_decision_trade_when_score_high(self):
        ind = make_ind()
        opp = self.engine.opportunity_score(ind)
        assert opp.score >= 70
        p = self.engine.build_proposal(ind, opp, 0.5, True)
        assert p.recommendation == FinalDecision.trade

    def test_decision_wait_on_high_impact_event(self):
        ind = make_ind(high_impact_event=True)
        p = self.engine.build_proposal(ind, self.engine.opportunity_score(ind), 0.5, True)
        assert p.recommendation == FinalDecision.wait

    def test_decision_reduce_risk_when_score_low(self):
        ind = make_ind(ema_fast=0, ema_slow=0, adx=5, rsi=50, macd_hist=-1, atr_pct=0.5, news_sentiment=-1)
        opp = self.engine.opportunity_score(ind)
        p = self.engine.build_proposal(ind, opp, 0.5, True)
        assert p.recommendation == FinalDecision.reduce_risk

    def test_confidence_reduced_by_event(self):
        ind = make_ind(high_impact_event=True)
        c_with = StrategyEngine._confidence(80, ind)
        c_without = StrategyEngine._confidence(80, make_ind(high_impact_event=False))
        assert c_with < c_without


class TestLimitLadder:
    def setup_method(self):
        self.engine = StrategyEngine()

    def test_proposal_includes_three_limit_levels(self):
        ind = make_ind()
        opp = self.engine.opportunity_score(ind)
        p = self.engine.build_proposal(ind, opp, 0.5, True)
        assert len(p.limit_levels) == 3
        assert sum(lv.risk_pct for lv in p.limit_levels) == 100.0

    def test_buy_limits_below_entry_with_sl_tp(self):
        ind = make_ind(price=2400.0, atr_pct=1.0)
        opp = self.engine.opportunity_score(ind)
        p = self.engine.build_proposal(ind, opp, 0.5, True, atr_multiple_sl=1.5, rr_target=2.0)
        sl_distance = 36.0  # 2400 * 1% * 1.5
        for lv, step, weight in zip(p.limit_levels, (0.25, 0.50, 0.75), (40.0, 35.0, 25.0)):
            assert lv.price == round(2400.0 - sl_distance * step, 5)
            assert lv.risk_pct == weight
            assert lv.sl < lv.price < lv.tp            # long: per-level SL below, TP above
            assert abs((lv.tp - lv.price) - (lv.price - lv.sl) * 2.0) < 0.05  # RR 1:2

    def test_sell_limits_above_entry_mirrored(self):
        ind = make_ind(price=2400.0, atr_pct=1.0)
        opp = self.engine.opportunity_score(ind)
        p = self.engine.build_proposal(ind, opp, 0.5, False, atr_multiple_sl=1.5, rr_target=2.0)
        sl_distance = 36.0
        for lv, step in zip(p.limit_levels, (0.25, 0.50, 0.75)):
            assert lv.price == round(2400.0 + sl_distance * step, 5)
            assert lv.tp < lv.price < lv.sl            # short: mirrored


# ---------------------------------------------------------------------------
# Risk Engine
# ---------------------------------------------------------------------------
from app.engine.risk_engine import PortfolioSnapshot, RiskConfig, RiskEngine


def make_snap(**kw) -> PortfolioSnapshot:
    base = dict(
        starting_capital=100_000.0,
        peak_equity=100_000.0,
        current_equity=100_000.0,
        realized_pnl_today=0.0,
        realized_pnl_week=0.0,
        realized_pnl_month=0.0,
        open_risk=0.0,
    )
    base.update(kw)
    return PortfolioSnapshot(**base)


class TestPortfolioSnapshotProps:
    def test_drawdown_zero_when_no_loss(self):
        assert make_snap().drawdown_pct == 0.0

    def test_drawdown_calculation(self):
        s = make_snap(peak_equity=110_000, current_equity=100_000)
        assert abs(s.drawdown_pct - 9.0909) < 0.01

    def test_drawdown_never_negative(self):
        s = make_snap(peak_equity=90_000, current_equity=100_000)
        assert s.drawdown_pct == 0.0

    def test_daily_loss_only_counts_losses(self):
        assert make_snap(realized_pnl_today=-500).daily_loss_pct == 0.5
        assert make_snap(realized_pnl_today=500).daily_loss_pct == 0.0


class TestRiskEngine:
    def setup_method(self):
        self.engine = RiskEngine(RiskConfig())  # 0.5/2/5/8/10 defaults

    def test_healthy_portfolio_passes(self):
        status = self.engine.check(make_snap())
        assert status.trading_paused is False
        assert status.risk_level == "low"
        assert "within limits" in status.message

    def test_daily_breach_pauses(self):
        status = self.engine.check(make_snap(realized_pnl_today=-2_500))  # 2.5% > 2%
        assert status.trading_paused is True
        assert status.risk_level == "critical"
        assert "Daily loss" in status.message

    def test_drawdown_breach_pauses(self):
        status = self.engine.check(make_snap(peak_equity=120_000, current_equity=105_000))  # 12.5% > 10
        assert status.trading_paused is True
        assert "Drawdown" in status.message

    def test_weekly_breach_pauses(self):
        status = self.engine.check(make_snap(realized_pnl_week=-6_000))  # 6% > 5%
        assert status.trading_paused is True
        assert "Weekly loss" in status.message

    def test_monthly_breach_pauses(self):
        status = self.engine.check(make_snap(realized_pnl_month=-9_000))  # 9% > 8%
        assert status.trading_paused is True

    def test_multiple_breaches_listed(self):
        status = self.engine.check(make_snap(realized_pnl_today=-2_500, realized_pnl_week=-6_000))
        assert status.message.count(";") >= 1
        assert "Daily loss" in status.message and "Weekly loss" in status.message

    def test_exactly_at_limit_breaches(self):
        # 2.0% == limit 2.0% → >= triggers
        status = self.engine.check(make_snap(realized_pnl_today=-2_000))
        assert status.trading_paused is True

    def test_open_risk_guard(self):
        # open risk 1.6% + 0.5% new trade > 2% daily limit
        status = self.engine.check(make_snap(open_risk=1_600))
        assert status.trading_paused is True
        assert "Open risk" in status.message

    def test_pause_resume_lifecycle(self):
        assert self.engine.is_paused is False
        self.engine.pause()
        assert self.engine.is_paused is True
        self.engine.resume()
        assert self.engine.is_paused is False

    def test_position_size_basic(self):
        # 0.5% of 100k = 500 risk; stop distance 50 → size 10
        assert self.engine.position_size(100_000, entry_price=100, stop_loss_price=50) == 10.0

    def test_position_size_zero_when_no_stop(self):
        assert self.engine.position_size(100_000, 100, 100) == 0.0

    def test_position_size_symmetric_for_short(self):
        assert self.engine.position_size(100_000, 50, 100) == 10.0

    def test_level_progression(self):
        assert self.engine.check(make_snap(current_equity=97_500)).risk_level == "medium"
        assert self.engine.check(make_snap(current_equity=94_000)).risk_level == "high"
        assert self.engine.check(make_snap(current_equity=90_000)).risk_level == "critical"

    def test_week_start_monday(self):
        d = datetime(2026, 9, 2, tzinfo=timezone.utc).date()  # Wednesday
        assert RiskEngine.week_start(d).weekday() == 0


# ---------------------------------------------------------------------------
# Portfolio Engine
# ---------------------------------------------------------------------------
from app.engine.portfolio_engine import BASE_WEIGHTS, PortfolioEngine
from app.models.schemas import AssetOpportunity, PortfolioInput


def make_opp(asset: str, score: float) -> AssetOpportunity:
    band = StrategyEngine.band_of(score)
    return AssetOpportunity(asset=asset, score=score, band=band, reasons=["test"])


class TestPortfolioEngine:
    def setup_method(self):
        self.engine = PortfolioEngine()

    def _input(self, profile="moderate"):
        return PortfolioInput(
            capital=100_000,
            target_return_pct=3.0,
            risk_profile=RiskProfile(profile),
            max_drawdown_pct=10.0,
            trading_mode="manual",
        )

    def test_weights_sum_to_100(self):
        rec = self.engine.recommend(self._input(), [])
        assert abs(sum(a.weight_pct for a in rec.allocation) - 100.0) < 0.5

    def test_moderate_baseline_allocation(self):
        rec = self.engine.recommend(self._input(), [])
        weights = {a.asset: a.weight_pct for a in rec.allocation}
        assert weights["XAUUSD"] == 35
        assert weights["Cash"] == 10

    def test_very_high_score_tilts_up(self):
        opps = [make_opp("XAUUSD", 85)]
        rec = self.engine.recommend(self._input(), opps)
        weights = {a.asset: a.weight_pct for a in rec.allocation}
        assert weights["XAUUSD"] == 45  # 35 + 10

    def test_low_score_tilts_down_and_cash_absorbs(self):
        opps = [make_opp("XAUUSD", 10)]
        rec = self.engine.recommend(self._input(), opps)
        weights = {a.asset: a.weight_pct for a in rec.allocation}
        assert weights["XAUUSD"] == 25  # 35 - 10
        assert weights["Cash"] == 20    # absorbs

    def test_never_negative_weight(self):
        opps = [make_opp(a, 5) for a in ("XAUUSD", "EURUSD", "USDJPY", "GBPUSD")]
        rec = self.engine.recommend(self._input("aggressive"), opps)
        assert all(a.weight_pct >= 0 for a in rec.allocation)

    def test_all_profiles_produce_valid_allocation(self):
        for profile in ("conservative", "moderate", "aggressive"):
            rec = self.engine.recommend(self._input(profile), [])
            assert 99.5 <= sum(a.weight_pct for a in rec.allocation) <= 100.5
            assert rec.expected_monthly_return_pct >= 0
            assert rec.expected_drawdown_pct >= 0

    def test_aggressive_higher_expected_return_than_conservative(self):
        cons = self.engine.recommend(self._input("conservative"), [])
        aggr = self.engine.recommend(self._input("aggressive"), [])
        assert aggr.expected_monthly_return_pct > cons.expected_monthly_return_pct

    def test_reasoning_mentions_target_gap_when_below(self):
        rec = self.engine.recommend(self._input(), [])
        assert any("ไม่ไล่ตามเป้า" in r or "เป้าหมาย" in r for r in rec.reasoning)

    def test_base_weights_all_sum_100(self):
        for profile, weights in BASE_WEIGHTS.items():
            assert sum(weights.values()) == 100, f"{profile} weights sum != 100"


# ---------------------------------------------------------------------------
# PaperBroker
# ---------------------------------------------------------------------------
from app.integrations.brokers import OrderRequest, PaperBroker


@pytest.mark.asyncio
class TestPaperBroker:
    async def test_connect_returns_true(self):
        broker = PaperBroker()
        assert await broker.connect() is True

    async def test_place_and_list_position(self):
        broker = PaperBroker()
        await broker.connect()
        res = await broker.place_order(OrderRequest(
            user_id="u1", asset="XAUUSD", direction="BUY",
            volume=0.1, entry_price=2400.0, stop_loss=2390.0, take_profit=2420.0,
        ))
        assert res.ok is True
        assert res.broker_order_id is not None
        positions = await broker.positions("u1")
        assert len(positions) == 1
        assert positions[0].asset == "XAUUSD"

    async def test_positions_filtered_by_user(self):
        broker = PaperBroker()
        for uid in ("u1", "u2"):
            await broker.place_order(OrderRequest(
                user_id=uid, asset="EURUSD", direction="BUY",
                volume=0.01, entry_price=1.085, stop_loss=None, take_profit=None,
            ))
        assert len(await broker.positions("u1")) == 1
        assert len(await broker.positions("u2")) == 1
        assert len(await broker.positions("ghost")) == 0

    async def test_close_position(self):
        broker = PaperBroker()
        res = await broker.place_order(OrderRequest(
            user_id="u1", asset="XAUUSD", direction="BUY",
            volume=0.1, entry_price=2400.0, stop_loss=None, take_profit=None,
        ))
        close = await broker.close_position(res.broker_order_id)
        assert close.ok is True
        assert await broker.positions("u1") == []

    async def test_close_unknown_ticket_fails(self):
        broker = PaperBroker()
        res = await broker.close_position("PAPER-999999")
        assert res.ok is False

    async def test_pnl_buy_positive_when_price_rises(self):
        broker = PaperBroker()
        res = await broker.place_order(OrderRequest(
            user_id="u1", asset="XAUUSD", direction="BUY",
            volume=1.0, entry_price=100.0, stop_loss=None, take_profit=None,
        ))
        pos = (await broker.positions("u1"))[0]
        pos.current_price = 110.0
        assert PaperBroker._approx_pnl(pos) == pytest.approx(1000.0)  # 100 oz/lot

    async def test_pnl_sell_positive_when_price_falls(self):
        broker = PaperBroker()
        await broker.place_order(OrderRequest(
            user_id="u1", asset="XAUUSD", direction="SELL",
            volume=1.0, entry_price=100.0, stop_loss=None, take_profit=None,
        ))
        pos = (await broker.positions("u1"))[0]
        pos.current_price = 90.0
        assert PaperBroker._approx_pnl(pos) == pytest.approx(1000.0)  # 100 oz/lot

    async def test_quote_known_and_unknown(self):
        broker = PaperBroker()
        assert await broker.quote("XAUUSD") == 2400.00
        assert await broker.quote("NOPE") == 0.0


# ---------------------------------------------------------------------------
# API routes (in-process via httpx ASGI transport)
# ---------------------------------------------------------------------------
from httpx import ASGITransport, AsyncClient

from app.integrations.brokers import PaperBroker
from app.integrations.line_client import LineClient
from app.main import app
from app.services.database import Database


@pytest.fixture
async def client():
    # ASGITransport does NOT run lifespan events — seed app.state manually.
    # (Production lifespan does this in main.py; Database()/LineClient() are
    #  no-op safe when Supabase/LINE creds are missing.)
    app.state.db = Database()
    app.state.line = LineClient()
    app.state.broker = PaperBroker()
    await app.state.broker.connect()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

@pytest.mark.asyncio
class TestApiRoutes:
    async def test_health(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"

    async def test_goal_assess_moderate(self, client):
        r = await client.post("/api/goal/assess", json={
            "capital": 100_000, "target_return_pct": 3, "risk_profile": "moderate",
            "max_drawdown_pct": 10, "trading_mode": "manual",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["expected_profit"] == 3000.0
        assert data["probability"] in ("high", "moderate", "low", "high_probability", "moderate_probability", "low_probability")
        assert len(data["scenarios"]) == 3

    async def test_goal_assess_validation_error(self, client):
        r = await client.post("/api/goal/assess", json={"capital": "abc"})
        assert r.status_code == 422

    async def test_goal_assess_rejects_negative_capital(self, client):
        r = await client.post("/api/goal/assess", json={
            "capital": -1000, "target_return_pct": 3, "risk_profile": "moderate",
            "max_drawdown_pct": 10, "trading_mode": "MANUAL",
        })
        assert r.status_code == 422

    async def test_market_summary(self, client):
        r = await client.get("/api/market/summary")
        assert r.status_code == 200
        data = r.json()
        assert "regime" in data and "opportunities" in data

    async def test_portfolio_recommend(self, client):
        r = await client.post("/api/portfolio/recommend", json={
            "capital": 100_000, "target_return_pct": 3, "risk_profile": "moderate",
            "max_drawdown_pct": 10, "trading_mode": "manual",
        })
        assert r.status_code == 200
        data = r.json()
        assert abs(sum(a["weight_pct"] for a in data["allocation"]) - 100) < 1

    async def test_risk_check_ok(self, client):
        r = await client.post("/api/risk/check", json={
            "starting_capital": 100_000, "peak_equity": 100_000,
            "current_equity": 100_000, "realized_pnl_today": 0,
            "realized_pnl_week": 0, "realized_pnl_month": 0, "open_risk": 0,
        })
        assert r.status_code == 200
        assert r.json()["trading_paused"] is False

    async def test_risk_check_breach(self, client):
        r = await client.post("/api/risk/check", json={
            "starting_capital": 100_000, "peak_equity": 100_000,
            "current_equity": 100_000, "realized_pnl_today": -3_000,
            "realized_pnl_week": 0, "realized_pnl_month": 0, "open_risk": 0,
        })
        assert r.status_code == 200
        assert r.json()["trading_paused"] is True

    async def test_signals_latest(self, client):
        r = await client.get("/api/signals/latest")
        assert r.status_code == 200

    async def test_chat_endpoint(self, client):
        r = await client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "สวัสดี วันนี้ควรเทรดไหม"}]
        })
        assert r.status_code == 200
        data = r.json()
        assert "reply" in data
        assert len(data["reply"]) > 0

    async def test_cors_headers_present(self, client):
        r = await client.options("/api/goal/assess", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        })
        assert r.status_code in (200, 204)
        assert "access-control-allow-origin" in {k.lower() for k in r.headers}

    async def test_openapi_docs_served(self, client):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json()["paths"]
        assert "/health" in paths
        assert "/api/goal/assess" in paths
