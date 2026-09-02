"""Goal Engine — assesses feasibility of the user's monthly return target.

Calculates Best/Normal/Worst case scenarios, probability classification and
emits a risk warning when the target implies drawdown beyond the user's limit.

Never guarantees profit — output is a probabilistic assessment only.
"""
from __future__ import annotations

from app.models.schemas import (
    GoalAssessment,
    GoalInput,
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


class GoalEngine:
    """Assess goal feasibility under risk constraints."""

    def assess(self, goal: GoalInput) -> GoalAssessment:
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

        return GoalAssessment(
            capital=goal.capital,
            target_return_pct=goal.target_return_pct,
            expected_profit=expected_profit,
            probability=probability,
            scenarios=scenarios,
            risk_warning=risk_warning,
            reasoning=self._reasoning(goal, probability, hi),
        )

    # ------------------------------------------------------------------
    @staticmethod
    def expected_profit(capital: float, target_return_pct: float) -> float:
        return round(capital * target_return_pct / 100.0, 2)

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
