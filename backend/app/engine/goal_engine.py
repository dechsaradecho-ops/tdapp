"""Goal Engine — assesses feasibility of the user's monthly return target.

Calculates Best/Normal/Worst case scenarios, probability classification and
emits a risk warning when the target implies drawdown beyond the user's limit.

Reality-aware (2026-09-04): when the caller supplies a GoalRealityContext the
base envelope math is ADJUSTED by the user's actual trading state — realized
PnL, win rate, current market regime, kill switch and manual pause. Without a
context the engine stays a pure deterministic calculator (backward compatible).

Never guarantees profit — output is a probabilistic assessment only.
"""
from __future__ import annotations

from app.models.schemas import (
    GoalAssessment,
    GoalInput,
    GoalRealityContext,
    Probability,
    Scenario,
)

# Realistic monthly return envelopes by risk profile (percent, non-guaranteed).
# (normal_low, normal_high, best_case, worst_case)
PROFILE_ENVELOPES: dict[str, tuple[float, float, float, float]] = {
    "conservative": (0.5, 1.5, 3.0, -4.0),
    "moderate": (1.0, 3.0, 6.0, -8.0),
    "aggressive": (2.0, 6.0, 12.0, -15.0),
}

# Reality adjustments (percentage points on the probability scale). Deliberately
# small and explainable — each one maps to a reasoning line the user can read.
WIN_RATE_BONUS_THRESHOLD = 55.0   # win rate ≥ 55% → +1 tier of confidence
WIN_RATE_PENALTY_THRESHOLD = 35.0 # win rate ≤ 35% → −1 tier
PNL_BONUS_THRESHOLD = 0.0         # realized PnL > 0 → +1 tier
PNL_PENALTY_THRESHOLD = 0.0       # realized PnL < 0 → −1 tier
BULL_REGIME_BONUS = True          # bull_trend/strong_bull → +1 tier
BEAR_REGIME_PENALTY = True        # bear_trend/strong_bear → −1 tier

_PROBABILITY_ORDER = [Probability.low, Probability.moderate, Probability.high]


def _shift_probability(base: Probability, steps: int) -> Probability:
    """Move the probability tier by `steps` (+1 = more confident), clamped."""
    idx = _PROBABILITY_ORDER.index(base)
    return _PROBABILITY_ORDER[max(0, min(len(_PROBABILITY_ORDER) - 1, idx + steps))]


class GoalEngine:
    """Assess goal feasibility under risk constraints."""

    def assess(self, goal: GoalInput,
               reality: GoalRealityContext | None = None) -> GoalAssessment:
        expected_profit = self.expected_profit(goal.capital, goal.target_return_pct)
        lo, hi, best, worst = PROFILE_ENVELOPES[goal.risk_profile.value]

        # Probability: how does the target compare to the profile's normal range?
        if goal.target_return_pct <= hi:
            probability = Probability.high
        elif goal.target_return_pct <= hi * 1.75:
            probability = Probability.moderate
        else:
            probability = Probability.low

        scenarios = self._scenarios(goal.capital, goal.target_return_pct, lo, hi, best, worst)

        risk_warning = None
        if probability != Probability.high or goal.target_return_pct > hi:
            risk_warning = self._build_warning(goal, worst)

        reasoning = self._reasoning(goal, probability, hi)

        # ---- reality adjustment (only when live state is available) --------
        if reality is not None and reality.data_available:
            probability, adj_reasons, blocked = self._apply_reality(
                probability, goal, reality, worst)
            reasoning.extend(adj_reasons)
            if blocked:
                # Trading is hard-blocked right now — the assessment must say so
                # regardless of how good the envelope math looks.
                risk_warning = (
                    f"⚠️ ระบบหยุดเทรดอยู่ตอนนี้ — "
                    + ("; ".join(reality.kill_triggers) if reality.kill_triggers
                       else reality.pause_reason or "manual pause")
                    + " — ประเมินนี้เป็นไปได้เมื่อระบบกลับมาเทรดได้แล้ว")

        return GoalAssessment(
            capital=goal.capital,
            target_return_pct=goal.target_return_pct,
            expected_profit=expected_profit,
            probability=probability,
            scenarios=scenarios,
            risk_warning=risk_warning,
            reasoning=reasoning,
            reality=reality,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def expected_profit(capital: float, target_return_pct: float) -> float:
        return round(capital * target_return_pct / 100.0, 2)

    def _apply_reality(
        self,
        probability: Probability,
        goal: GoalInput,
        reality: GoalRealityContext,
        worst: float,
    ) -> tuple[Probability, list[str], bool]:
        """Fold live portfolio/market state into the probability tier.

        Returns (new_probability, reasoning_lines, hard_blocked).
        Each adjustment is one explainable reasoning line — no black box.
        """
        reasons: list[str] = []
        steps = 0

        # 1) Realized PnL — the most honest signal of how trading is going
        if reality.pnl_total > PNL_BONUS_THRESHOLD:
            steps += 1
            reasons.append(
                f"📊 สถิติจริง: PnL รวม +{reality.pnl_total:,.2f} (บวก) → ปรับความเป็นไปได้ขึ้น 1 ระดับ")
        elif reality.pnl_total < PNL_PENALTY_THRESHOLD:
            steps -= 1
            reasons.append(
                f"📊 สถิติจริง: PnL รวม {reality.pnl_total:,.2f} (ติดลบ) → ปรับความเป็นไปได้ลง 1 ระดับ")

        # 2) Win rate — only meaningful with a few closed trades
        if reality.closed_count >= 3:
            if reality.win_rate >= WIN_RATE_BONUS_THRESHOLD:
                steps += 1
                reasons.append(
                    f"📊 สถิติจริง: Win Rate {reality.win_rate:.0f}% จาก {reality.closed_count} ไม้ที่ปิดแล้ว (สูง) → ปรับขึ้น 1 ระดับ")
            elif reality.win_rate <= WIN_RATE_PENALTY_THRESHOLD:
                steps -= 1
                reasons.append(
                    f"📊 สถิติจริง: Win Rate {reality.win_rate:.0f}% จาก {reality.closed_count} ไม้ที่ปิดแล้ว (ต่ำ) → ปรับลง 1 ระดับ")

        # 3) Market regime — trending market helps trend-following systems
        regime = (reality.market_regime or "").lower()
        if BULL_REGIME_BONUS and regime in ("bull_trend", "strong_bull_trend"):
            steps += 1
            reasons.append(
                f"📈 ตลาดจริงตอนนี้: {regime} ({reality.market_sentiment}) → ระบบเทรดตามเทรนด์ได้เปรียบ → ปรับขึ้น 1 ระดับ")
        elif BEAR_REGIME_PENALTY and regime in ("bear_trend", "strong_bear_trend"):
            steps -= 1
            reasons.append(
                f"📉 ตลาดจริงตอนนี้: {regime} ({reality.market_sentiment}) → สวนทางกับระบบ → ปรับลง 1 ระดับ")

        # 4) Kill switch / manual pause — hard block, cannot be offset by bonuses
        blocked = bool(reality.kill_switch_engaged or reality.trading_paused)
        if reality.kill_switch_engaged:
            steps = min(steps, 0)  # a kill switch cancels positive adjustments
            reasons.append(
                "🛑 Kill Switch ทำงานอยู่ — ระบบหยุดเทรดทั้งหมดชั่วคราว (ผลประเมินไม่นับระดับบวกจากสถิติ)")
        if reality.trading_paused:
            steps = min(steps, 0)
            reasons.append(
                f"⏸️ เทรดถูกหยุดด้วยตนเอง{(' — ' + reality.pause_reason) if reality.pause_reason else ''}")

        return _shift_probability(probability, steps), reasons, blocked

    def _scenarios(
        self,
        capital: float,
        target: float,
        lo: float,
        hi: float,
        best: float,
        worst: float,
    ) -> list[Scenario]:
        normal_pct = min(max(target, lo), hi)
        # Worst case is bounded by the user's max drawdown tolerance, not by history.
        return [
            Scenario(
                label="best_case",
                expected_return_pct=best,
                expected_profit=round(capital * best / 100.0, 2),
                expected_drawdown_pct=abs(worst) * 0.6,
                note="Strong regime alignment: trending market + high opportunity scores + news tailwind.",
            ),
            Scenario(
                label="normal_case",
                expected_return_pct=round(normal_pct, 2),
                expected_profit=self.expected_profit(capital, normal_pct),
                expected_drawdown_pct=abs(worst) * 0.45,
                note="Typical conditions with the configured risk-per-trade and diversification.",
            ),
            Scenario(
                label="worst_case",
                expected_return_pct=worst,
                expected_profit=round(capital * worst / 100.0, 2),
                expected_drawdown_pct=abs(worst),
                note="Adverse regime: choppy/news-driven market, consecutive stop-outs. Risk engine pauses trading at limits.",
            ),
        ]

    def _build_warning(self, goal: GoalInput, worst: float) -> str:
        return (
            f"Risk Warning: target {goal.target_return_pct:.1f}% monthly with a "
            f"{goal.risk_profile.value} profile implies elevated expected drawdown "
            f"(worst case ~{abs(worst):.1f}% vs your max drawdown {goal.max_drawdown_pct:.1f}%). "
            f"Capital at risk in the worst case ≈ {self.expected_profit(goal.capital, worst):,.0f}. "
            "This is a probabilistic estimate — success is NOT guaranteed. Consider lowering the "
            "target, reducing risk per trade, or accepting a longer time horizon."
        )

    @staticmethod
    def _reasoning(goal: GoalInput, probability: Probability, hi: float) -> list[str]:
        base = [
            f"Target profit = {goal.target_return_pct:.1f}% × {goal.capital:,.0f} = "
            f"{GoalEngine.expected_profit(goal.capital, goal.target_return_pct):,.0f} per month.",
            f"Profile '{goal.risk_profile.value}' normally delivers {hi:.1f}% monthly at best in normal conditions.",
            f"Max drawdown budget {goal.max_drawdown_pct:.1f}% caps how aggressively the target can be pursued.",
        ]
        if probability == Probability.high:
            base.append("Target sits inside the profile's normal range → High Probability.")
        elif probability == Probability.moderate:
            base.append("Target exceeds the normal range but is still reachable in favorable regimes → Moderate Probability.")
        else:
            base.append("Target is far above what this risk profile can statistically deliver → Low Probability.")
        base.append("All figures are probabilistic estimates; the platform never guarantees profit.")
        return base
