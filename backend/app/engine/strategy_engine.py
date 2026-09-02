"""AI Strategy Engine — combines trend, momentum, volatility, news and sentiment inputs
into an opportunity score (0-100) and an explainable trading proposal.

Indicator inputs (EMA, ADX, Supertrend, RSI, MACD, ATR, ...) are supplied by the
Market Scanner worker; this engine only scores/explains, keeping it testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.models.schemas import (
    AssetOpportunity,
    FinalDecision,
    OpportunityBand,
    RiskProfile,
    SignalProposal,
)


@dataclass
class IndicatorSnapshot:
    """Values computed upstream (worker #1 / data feed) for one asset."""

    asset: str
    price: float

    # Trend
    ema_fast: float = 0.0          # e.g. EMA50
    ema_slow: float = 0.0          # e.g. EMA200
    adx: float = 0.0               # >25 = trending
    supertrend_dir: int = 0        # +1 up / -1 down

    # Momentum
    rsi: float = 50.0              # 0-100
    macd_hist: float = 0.0         # >0 bullish
    price_change_pct_20: float = 0.0

    # Volatility
    atr_pct: float = 0.0           # ATR / price * 100
    volatility_index: float = 0.0  # e.g. GVZ for gold, VIX for indices

    # News / sentiment (from worker #2)
    news_sentiment: float = 0.0    # -1 .. +1
    high_impact_event: bool = False

    source: str = "demo"           # "live" (quote feed) / "demo" (random-walk fallback)

    reasons: list[str] = field(default_factory=list)


BANDS = ((81.0, 100.0, OpportunityBand.very_high), (61.0, 81.0, OpportunityBand.high),
         (31.0, 61.0, OpportunityBand.medium), (0.0, 31.0, OpportunityBand.low))


class StrategyEngine:
    """Deterministic scoring core + explainability. AI layer wraps this for narrative."""

    # ------------------------------------------------------------------
    def opportunity_score(self, ind: IndicatorSnapshot) -> AssetOpportunity:
        score = 0.0
        reasons: list[str] = []

        # --- Trend component (0-35) ---
        if ind.ema_fast and ind.ema_slow:
            if ind.ema_fast > ind.ema_slow:
                score += 20
                reasons.append("EMA50 สูงกว่า EMA200 → แนวโน้มขาขึ้น")
            else:
                score -= 5
                reasons.append("EMA50 ต่ำกว่า EMA200 → แนวโน้มขาลง")
        if ind.adx >= 25:
            score += 15
            reasons.append(f"ADX = {ind.adx:.0f} (≥25) → มีความแข็งแรงของเทรนด์")
        elif ind.adx > 0:
            score += 5
            reasons.append(f"ADX = {ind.adx:.0f} (<25) → เทรนด์อ่อน/ไซด์เวย์")

        # --- Momentum (0-25) ---
        if ind.rsi >= 55 and ind.rsi <= 70:
            score += 15
            reasons.append(f"RSI = {ind.rsi:.0f} → โมเมนตัมเป็นบวกแบบไม่ร้อนเกินไป")
        elif ind.rsi > 70:
            score += 5
            reasons.append(f"RSI = {ind.rsi:.0f} → overbought เสี่ยงย่อ")
        elif ind.rsi < 45 and ind.supertrend_dir > 0:
            score += 8
            reasons.append(f"RSI = {ind.rsi:.0f} แต่ Supertrend ยังขาขึ้น → โอกาส buy on dip")
        if ind.macd_hist > 0:
            score += 10
            reasons.append("MACD histogram เป็นบวก → โมเมนตัมยืนยันทิศทาง")

        # --- Volatility (0-20): moderate volatility is ideal ---
        if 0.4 <= ind.atr_pct <= 1.5:
            score += 15
            reasons.append(f"ATR {ind.atr_pct:.2f}% → ความผันผวนเหมาะสมต่อการเทรด")
        elif ind.atr_pct > 2.5:
            score -= 10
            reasons.append(f"ATR {ind.atr_pct:.2f}% → ผันผวนสูงเกิน ปรับลดขนาดโพซิชัน")
        else:
            score += 5

        # --- News & sentiment (0-20) ---
        score += max(-10.0, min(10.0, ind.news_sentiment * 10))
        if ind.high_impact_event:
            score -= 5
            reasons.append("มีข่าว impact สูงใกล้ตัว → ความเสี่ยง spike, รอให้ตลาดนิ่งก่อน")
        else:
            score += 5
            reasons.append("ไม่มีข่าว impact สูงระยะสั้น → สภาพแวดล้อมคาดการณ์ได้")

        if ind.supertrend_dir == 1 and ind.macd_hist > 0:
            score += 5
            reasons.append("Supertrend + MACD ยืนยันทิศเดียวกัน")

        score = max(0.0, min(100.0, score))
        return AssetOpportunity(
            asset=ind.asset, score=round(score, 1),
            band=self.band_of(score), reasons=reasons or ["ข้อมูลไม่เพียงพอ — คะแนนกลาง"],
        )

    @staticmethod
    def band_of(score: float) -> OpportunityBand:
        for lo, hi, band in BANDS:
            if lo <= score < hi or (hi == 100.0 and score == 100.0):
                return band
        return OpportunityBand.low

    # ------------------------------------------------------------------
    def build_proposal(
        self,
        ind: IndicatorSnapshot,
        opp: AssetOpportunity,
        risk_per_trade_pct: float,
        regime_bullish: bool,
        atr_multiple_sl: float = 1.5,
        rr_target: float = 2.0,
        risk_profile: RiskProfile = RiskProfile.moderate,
    ) -> SignalProposal:
        """Turn the scored snapshot into an explainable BUY/SELL proposal with SL/TP."""
        direction = "BUY" if regime_bullish else "SELL"
        sign = 1 if regime_bullish else -1
        sl_distance = max(ind.price * ind.atr_pct / 100.0 * atr_multiple_sl, ind.price * 0.001)
        stop_loss = ind.price - sign * sl_distance
        take_profit = ind.price + sign * sl_distance * rr_target
        confidence = self._confidence(opp.score, ind)

        reasons = list(ind.reasons) or list(opp.reasons)
        reasons.insert(0, f"Opportunity Score {opp.score:.0f}/100 ({opp.band.value})")

        decision = self._decision(opp.score, ind)

        return SignalProposal(
            asset=ind.asset,
            direction=direction,
            confidence=confidence,
            entry=round(ind.price, 5),
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            expected_rr=rr_target,
            risk_per_trade_pct=risk_per_trade_pct,
            reason=reasons[:6],
            recommendation=decision,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _confidence(score: float, ind: IndicatorSnapshot) -> float:
        base = min(score, 90.0)
        if ind.high_impact_event:
            base -= 10
        return round(max(10.0, base), 1)

    @staticmethod
    def _decision(opp_score: float, ind: IndicatorSnapshot) -> FinalDecision:
        if ind.high_impact_event:
            return FinalDecision.wait
        if opp_score >= 70:
            return FinalDecision.trade
        if opp_score >= 50:
            return FinalDecision.wait
        return FinalDecision.reduce_risk
