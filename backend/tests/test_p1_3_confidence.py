"""P1-3 — Confidence is INDEPENDENT of Opportunity Score.

The audit finding this suite locks down: ``market_scanner`` wrote
``"confidence": opp.score`` into every ``market_analysis`` row AND gated
signal generation on ``opp.score >= min_confidence``. "Confidence" and
"Opportunity Score" were therefore the SAME NUMBER, and the user's
``min_opportunity`` setting was never enforced at generation time (only the
execution RiskOfficer compared it). The owner could not say
"I want strong setups that are ALSO confirmed by many independent signals".

P1-3 splits the two axes:
  * ``opportunity_score``  = weighted SETUP QUALITY (0-100),
  * ``evidence_confidence`` = how many INDEPENDENT sources AGREE with the
    wanted side (0-100),
  * the scanner gate now requires BOTH thresholds.

Tests here prove the axes are genuinely independent (no monotone transform
of one another), that both gates are enforced, that the model is
direction-symmetric, explainable, and fail-closed on a high-impact event.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_p1_3_confidence.py -v
"""
from __future__ import annotations

import pytest

from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
from app.models.schemas import (
    AppSettings,
    AssetOpportunity,
    effective_min_confidence,
    effective_min_opportunity,
)
from app.workers import market_scanner
from tests.test_workers import FakeDatabase, strong_snapshot, choppy_snapshot


ENGINE = StrategyEngine()


def _ind(**overrides) -> IndicatorSnapshot:
    """A clean, fully-agreeing BUY snapshot; override fields to break agreement.

    Defaults are deliberately AGREEMENT-MAXIMAL (every independent source
    confirms BUY) so a single override isolates that source's contribution.
    """
    base = dict(
        asset="EURUSD", price=1.1000, ema_fast=1.1100, ema_slow=1.1000,
        adx=32.0, supertrend_dir=1, rsi=50.0, macd_hist=2.0, atr_pct=0.9,
        volatility_index=12.0, news_sentiment=0.5, high_impact_event=False,
        source="live",
    )
    base.update(overrides)
    return IndicatorSnapshot(**base)


# --------------------------------------------------------------- independence
class TestAxesAreIndependent:
    def test_confidence_is_not_the_opportunity_score(self):
        """The whole point: the two numbers must be able to DIFFER.

        Pre-P1-3 the scanner wrote ``confidence = opp.score`` — a monotone
        copy. Now the same score can carry different confidence.
        """
        opp = ENGINE.opportunity_score(_ind())
        assert opp.confidence != opp.score

    def test_same_score_different_confidence(self):
        """Two setups with an IDENTICAL opportunity score but different
        evidence agreement must yield different confidence — proving
        confidence is NOT a monotone function of score.

        ``rsi=75`` pushes RSI out of the supporting zone (agreement −1);
        ``high_impact_event=True`` folds a hard −30. Both land on score 80
        yet must not collapse to the same confidence.
        """
        rsi_out = ENGINE.opportunity_score(_ind(rsi=75.0))
        event = ENGINE.opportunity_score(_ind(high_impact_event=True))
        # The two setups are constructed to share the SAME quality score ...
        assert rsi_out.score == event.score, (
            f"fixture drift: {rsi_out.score} != {event.score}")
        # ... but the news event must be materially LESS reliable.
        assert event.confidence < rsi_out.confidence

    def test_confidence_tracks_agreement_not_score_magnitude(self):
        """Breaking an independent source lowers confidence even when the
        opportunity score barely moves (RSI disagreement −1 flips the whole
        RSI-zone term)."""
        clean = ENGINE.opportunity_score(_ind())
        broken = ENGINE.opportunity_score(_ind(rsi=75.0))
        assert broken.confidence < clean.confidence

    def test_quality_anchor_caps_confidence_at_45_without_agreement(self):
        """A great raw score can never max out confidence on its own — the
        score contributes at most 45 (``min(score, 45)``)."""
        # Remove EVERY agreement source (no EMA/ADX/Supertrend/MACD/RSI/sent).
        ind = _ind(ema_fast=0.0, ema_slow=0.0, adx=0.0, supertrend_dir=0,
                   macd_hist=0.0, rsi=0.0, news_sentiment=0.0)
        conf, reasons = ENGINE.evidence_confidence(100.0, ind)
        assert conf <= 45.0
        assert reasons and reasons[0].startswith("ฐานจากคุณภาพเซ็ตอัป")

    def test_higher_agreement_yields_higher_confidence(self):
        """Monotone in AGREEMENT: fixing a contradicting source to agree
        raises confidence."""
        bad = ENGINE.evidence_confidence(80.0, _ind(rsi=75.0, macd_hist=-2.0))
        good = ENGINE.evidence_confidence(80.0, _ind(rsi=50.0, macd_hist=2.0))
        assert good[0] > bad[0]

    def test_opportunity_score_populates_confidence_field(self):
        """``opportunity_score`` must return a populated ``AssetOpportunity``
        whose ``confidence`` equals a standalone ``evidence_confidence`` call
        for the SAME resolved direction."""
        ind = _ind()
        opp = ENGINE.opportunity_score(ind)
        assert isinstance(opp, AssetOpportunity)
        assert 10.0 <= opp.confidence <= 100.0
        assert opp.confidence_reasons, "confidence must be explainable"

        # Same math reachable directly (BUY is the auto-resolved side here).
        direct, _ = ENGINE.evidence_confidence(opp.score, ind, direction="BUY")
        assert opp.confidence == direct


# ---------------------------------------------------------------- threshold fn
class TestEffectiveMinOpportunity:
    def test_returns_settings_value(self):
        s = AppSettings(min_opportunity=72.0)
        assert effective_min_opportunity(s, "EURUSD") == 72.0

    def test_returns_value_for_all_assets(self):
        """No gold-specific override table yet — every asset uses the base."""
        s = AppSettings(min_opportunity=55.0)
        assert effective_min_opportunity(s, "XAUUSD") == 55.0
        assert effective_min_opportunity(s, "GBPJPY") == 55.0

    def test_none_disables_the_gate(self):
        """A None min_opportunity is treated as "no minimum" (0.0), matching
        the helper's defensive ``getattr(..., 60.0) or 0.0`` — it must never
        raise (unlike ``effective_min_confidence`` which requires a value)."""
        s = AppSettings()
        assert s.min_opportunity == 60.0
        s.min_opportunity = None  # type: ignore[assignment]
        assert effective_min_opportunity(s, "EURUSD") == 0.0

    def test_independent_of_min_confidence(self):
        s = AppSettings(min_confidence=30.0, min_opportunity=90.0)
        assert effective_min_confidence(s, "EURUSD") == 30.0
        assert effective_min_opportunity(s, "EURUSD") == 90.0


# ------------------------------------------------------------------- symmetry
class TestDirectionSymmetry:
    def test_buy_and_sell_mirror_confidence(self):
        """A bullish setup scored BUY and its mirror-image bearish setup
        scored SELL must reach the SAME confidence (no long bias)."""
        buy = _ind()  # up-trend, agrees with BUY
        sell = _ind(ema_fast=1.0900, ema_slow=1.1000, supertrend_dir=-1,
                    rsi=45.0, macd_hist=-2.0, news_sentiment=-0.5)
        c_buy, _ = ENGINE.evidence_confidence(80.0, buy, direction="BUY")
        c_sell, _ = ENGINE.evidence_confidence(80.0, sell, direction="SELL")
        assert c_buy == c_sell

    def test_sell_scored_with_buy_direction_is_penalised(self):
        """Direction-awareness: the SAME bearish snapshot is less reliable
        when (wrongly) asked to confirm a BUY."""
        bearish = _ind(ema_fast=1.0900, ema_slow=1.1000, supertrend_dir=-1,
                       rsi=45.0, macd_hist=-2.0, news_sentiment=-0.5)
        c_sell, _ = ENGINE.evidence_confidence(80.0, bearish, direction="SELL")
        c_buy, _ = ENGINE.evidence_confidence(80.0, bearish, direction="BUY")
        assert c_buy < c_sell

    def test_opportunity_score_symmetric_buy_vs_sell(self):
        """The full scorer is symmetric: the mirror-image SELL setup scores
        the same as the BUY setup."""
        buy = ENGINE.opportunity_score(_ind(), direction="BUY")
        sell = ENGINE.opportunity_score(
            _ind(ema_fast=1.0900, ema_slow=1.1000, supertrend_dir=-1,
                 rsi=45.0, macd_hist=-2.0, news_sentiment=-0.5),
            direction="SELL")
        assert buy.score == sell.score
        assert buy.confidence == sell.confidence


# ----------------------------------------------------------------- fail-closed
class TestFailClosed:
    def test_high_impact_event_lowers_confidence_hard(self):
        clean = ENGINE.evidence_confidence(80.0, _ind())
        risky = ENGINE.evidence_confidence(80.0, _ind(high_impact_event=True))
        assert risky[0] <= clean[0] - 29.0, (
            "high-impact news must apply a hard reliability penalty")

    def test_confidence_never_below_floor(self):
        """Even a fully-contradicting, high-impact setup keeps a 10 floor
        (never negative) so downstream comparisons stay sane."""
        worst = _ind(ema_fast=1.0900, ema_slow=1.1000, supertrend_dir=-1,
                     adx=5.0, rsi=85.0, macd_hist=-2.0, news_sentiment=-1.0,
                     high_impact_event=True)
        conf, _ = ENGINE.evidence_confidence(0.0, worst, direction="BUY")
        assert conf == 10.0

    def test_confidence_never_above_100(self):
        conf, _ = ENGINE.evidence_confidence(100.0, _ind(
            breakout_state=2.0, breakout_level=1.09))
        assert conf == 100.0


# -------------------------------------------------------------- explainability
class TestExplainability:
    def test_reasons_name_each_source(self):
        _, reasons = ENGINE.evidence_confidence(80.0, _ind())
        blob = " ".join(reasons)
        assert "EMA" in blob and "ADX" in blob and "Supertrend" in blob
        assert "MACD" in blob and "RSI" in blob and "Sentiment" in blob

    def test_agreement_line_reports_ratio(self):
        _, reasons = ENGINE.evidence_confidence(80.0, _ind())
        agree_lines = [r for r in reasons if "หลักฐานเห็นด้วย" in r]
        assert agree_lines, "must report the agreement ratio"
        assert "6/6" in agree_lines[0], agree_lines[0]

    def test_disagreement_is_labelled(self):
        _, reasons = ENGINE.evidence_confidence(80.0, _ind(rsi=75.0))
        assert any("สวนทาง" in r or "อยู่นอกโซน" in r for r in reasons)

    def test_high_impact_reason_present(self):
        _, reasons = ENGINE.evidence_confidence(80.0, _ind(
            high_impact_event=True))
        assert any("impact สูง" in r for r in reasons)


# --------------------------------------------------------------- legacy shim
class TestLegacyShim:
    def test_confidence_shim_matches_evidence_confidence(self):
        ind = _ind()
        assert ENGINE._confidence(80.0, ind) == \
            ENGINE.evidence_confidence(80.0, ind)[0]

    def test_shim_reduced_by_event(self):
        """Mirrors the historical test_confidence_reduced_by_event contract."""
        clean = ENGINE._confidence(80.0, _ind())
        risky = ENGINE._confidence(80.0, _ind(high_impact_event=True))
        assert risky < clean


# ------------------------------------------------------- scanner gate (BOTH)
class _GateHarness:
    """Drive ``scan_once`` with one asset + controllable thresholds."""

    def __init__(self, monkeypatch, snapshot_factory):
        self.db = FakeDatabase()
        self._snapshot_factory = snapshot_factory
        monkeypatch.setattr(market_scanner, "_snapshot_for",
                            self._snap)
        monkeypatch.setattr(market_scanner, "_market_closed",
                            lambda now=None: False)

        async def _spot(assets, **_kw):
            return {a: 101.0 for a in assets}, {}
        monkeypatch.setattr(market_scanner.quotes, "fetch_spot_prices", _spot)

    async def _snap(self, asset, news_sentiment=0.0):
        return self._snapshot_factory(asset)

    async def run(self, monkeypatch, settings: AppSettings):
        monkeypatch.setattr(market_scanner, "get_app_settings",
                            lambda _db: settings)
        await market_scanner.scan_once(self.db)
        return {row["asset"] for table, row in self.db.inserted
                if table == "signals"}


class TestScannerGateRequiresBoth:
    @pytest.mark.asyncio
    async def test_passes_when_both_thresholds_met(self, monkeypatch):
        h = _GateHarness(monkeypatch, lambda a: strong_snapshot(a))
        s = AppSettings(allowed_assets=["EURUSD"], min_confidence=50.0,
                        min_opportunity=50.0)
        assert await h.run(monkeypatch, s) == {"EURUSD"}

    @pytest.mark.asyncio
    async def test_blocked_when_opportunity_below_min(self, monkeypatch):
        """A setup whose CONFIDENCE clears the bar must still be blocked when
        its OPPORTUNITY score does not — the old code ignored min_opportunity
        at generation time."""
        h = _GateHarness(monkeypatch, lambda a: strong_snapshot(a))
        s = AppSettings(allowed_assets=["EURUSD"], min_confidence=50.0,
                        min_opportunity=99.0)
        assert await h.run(monkeypatch, s) == set()

    @pytest.mark.asyncio
    async def test_blocked_when_confidence_below_min(self, monkeypatch):
        """The mirror: a setup whose OPPORTUNITY score clears the bar must
        still be blocked when its EVIDENCE CONFIDENCE does not.

        Fixture: MACD contradicts + RSI out of zone (score 60, confidence
        91.7) with min_confidence=95 → confidence fails, opportunity passes.
        """
        def snap(asset):
            return _ind(asset=asset, rsi=75.0, macd_hist=-2.0,
                        breakout_state=2.0, breakout_level=1.09)
        h = _GateHarness(monkeypatch, snap)
        s = AppSettings(allowed_assets=["EURUSD"], min_confidence=95.0,
                        min_opportunity=50.0)
        assert await h.run(monkeypatch, s) == set()

    @pytest.mark.asyncio
    async def test_confidence_gate_passes_same_setup_when_lowered(
            self, monkeypatch):
        """Control for the test above: the SAME snapshot emits once
        min_confidence drops below its actual confidence (91.7)."""
        def snap(asset):
            return _ind(asset=asset, rsi=75.0, macd_hist=-2.0,
                        breakout_state=2.0, breakout_level=1.09)
        h = _GateHarness(monkeypatch, snap)
        s = AppSettings(allowed_assets=["EURUSD"], min_confidence=50.0,
                        min_opportunity=50.0)
        assert await h.run(monkeypatch, s) == {"EURUSD"}

    @pytest.mark.asyncio
    async def test_weak_setup_emits_nothing(self, monkeypatch):
        """A choppy market (ADX 12, no Supertrend, flat RSI) fails the
        QUALITY gate: its opportunity score is below min_opportunity."""
        h = _GateHarness(monkeypatch, lambda a: choppy_snapshot(a))
        s = AppSettings(allowed_assets=["EURUSD"], min_confidence=50.0,
                        min_opportunity=70.0)
        assert await h.run(monkeypatch, s) == set()

    @pytest.mark.asyncio
    async def test_market_analysis_writes_evidence_confidence(self, monkeypatch):
        """``market_analysis.confidence`` must be the EVIDENCE confidence, not
        a copy of the opportunity score."""
        h = _GateHarness(monkeypatch, lambda a: strong_snapshot(a))
        s = AppSettings(allowed_assets=["EURUSD"], min_confidence=50.0,
                        min_opportunity=50.0)
        await h.run(monkeypatch, s)
        rows = [row for table, row in h.db.inserted
                if table == "market_analysis"]
        assert rows, "analysis must be written for priceable assets"
        row = next(r for r in rows if r["asset"] == "EURUSD")
        ind = strong_snapshot("EURUSD")
        opp = ENGINE.opportunity_score(ind, direction="BUY")
        assert row["confidence"] == opp.confidence
        # ... and it must NOT be the raw score (the pre-P1-3 bug).
        assert row["confidence"] != opp.score
