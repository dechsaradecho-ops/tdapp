"""Portfolio Engine — allocates capital across assets according to risk profile
and current opportunity scores. Weights always sum to 100% (Cash absorbs rounding).
"""
from __future__ import annotations

from app.models.schemas import (
    AllocationItem,
    AssetOpportunity,
    OpportunityBand,
    PortfolioInput,
    PortfolioRecommendation,
    RiskProfile,
)

# Base weights (before opportunity tilt) per profile
BASE_WEIGHTS: dict[RiskProfile, dict[str, float]] = {
    RiskProfile.conservative: {"XAUUSD": 20, "EURUSD": 15, "USDJPY": 10, "GBPUSD": 5, "Cash": 50},
    RiskProfile.moderate: {"XAUUSD": 35, "EURUSD": 25, "USDJPY": 20, "GBPUSD": 10, "Cash": 10},
    RiskProfile.aggressive: {"XAUUSD": 45, "EURUSD": 25, "USDJPY": 20, "GBPUSD": 10, "Cash": 0},
}

BAND_TILT = {
    OpportunityBand.very_high: 10,
    OpportunityBand.high: 5,
    OpportunityBand.medium: 0,
    OpportunityBand.low: -10,
}


class PortfolioEngine:
    """Opportunity-tilted allocation; conservatively clipped by profile caps."""

    def recommend(
        self,
        portfolio: PortfolioInput,
        opportunities: list[AssetOpportunity],
    ) -> PortfolioRecommendation:
        base = BASE_WEIGHTS[portfolio.risk_profile]
        scores = {o.asset: o for o in opportunities}
        # Tradable universe: payload.allowed_assets (Settings) when provided,
        # otherwise the legacy fixed BASE_WEIGHTS universe (keeps old
        # clients/tests on the exact baseline weights).
        raw_allowed = getattr(portfolio, "allowed_assets", None)
        if raw_allowed:
            seen: set[str] = set()
            universe: list[str] = []
            for a in raw_allowed:
                u = str(a or "").upper()
                if not u or u == "CASH" or u in seen:
                    continue
                seen.add(u)
                universe.append(u)
        else:
            universe = [k for k in base.keys() if k != "Cash"]
        if not universe:
            # Sanitized to nothing (e.g. ["Cash"]) → legacy universe.
            universe = [k for k in base.keys() if k != "Cash"]
        # Baseline prior per asset: known priors keep their BASE_WEIGHTS
        # value; pairs outside the legacy 4 (e.g. AUDUSD, EURJPY) get the
        # average risk-asset prior so newcomers enter on equal footing.
        base_risk = {k: v for k, v in base.items() if k != "Cash"}
        base_cash = float(base.get("Cash", 0.0))
        avg_prior = (sum(base_risk.values()) / len(base_risk)) if base_risk else 0.0
        priors: dict[str, float] = {
            a: float(base_risk.get(a, avg_prior)) for a in universe
        }

        tilted: dict[str, float] = {}
        rationale: dict[str, str] = {}
        for asset, weight in priors.items():
            opp = scores.get(asset)
            tilt = BAND_TILT[opp.band] if opp else 0
            tilted[asset] = max(0.0, weight + tilt)
            if opp:
                rationale[asset] = (
                    f"Opportunity Score {opp.score:.0f} ({opp.band.value}) → "
                    f"{'เพิ่ม' if tilt > 0 else 'ลด' if tilt < 0 else 'คง'}น้ำหนักจาก baseline {weight:g}%"
                )
            else:
                rationale[asset] = f"ยังไม่มีคะแนนล่าสุด ใช้ baseline {weight:g}%"
        # Cash starts at the profile prior, then absorbs the remainder so
        # total == 100 (legacy behaviour preserved exactly when no
        # allowed_assets is passed).
        tilted["Cash"] = float(base_cash)
        rationale["Cash"] = "สำรองเงินสดเพื่อกันความเสี่ยงและรอโอกาสที่ดีกว่า"

        # Cash absorbs over-allocation so total == 100
        risk_sum = sum(v for k, v in tilted.items() if k != "Cash")
        if risk_sum > 100:
            scale = (100 - tilted.get("Cash", 0)) / risk_sum
            for k in tilted:
                if k != "Cash":
                    tilted[k] = round(tilted[k] * scale, 1)
        tilted["Cash"] = round(100.0 - sum(v for k, v in tilted.items() if k != "Cash"), 1)
        tilted = {k: v for k, v in tilted.items() if v > 0}

        allocation = [
            AllocationItem(asset=k, weight_pct=v, rationale=rationale.get(k, ""))
            for k, v in sorted(tilted.items(), key=lambda kv: -kv[1])
        ]

        exp_return, exp_dd = self._expected(profile=portfolio.risk_profile, allocation=allocation)
        reasoning = [
            "จัดสรรตาม risk profile ก่อน แล้วปรับน้ำหนัก (tilt) ตาม Opportunity Score ล่าสุด",
            f"รวมน้ำหนักทุกสินทรัพย์ = {sum(a.weight_pct for a in allocation):.0f}% (Cash รับส่วนที่เหลือเสมอ)",
            f"เป้าหมาย {portfolio.target_return_pct:.1f}%/เดือน vs ผลตอบแทนคาดการณ์ {exp_return:.1f}% "
            f"— เน้นรักษาเงินต้นเป็นอันดับแรก",
            f"Expected drawdown {exp_dd:.1f}% ภายใต้ max drawdown ที่กำหนด {portfolio.max_drawdown_pct:.1f}%",
        ]
        if exp_return < portfolio.target_return_pct:
            reasoning.append(
                "การจัดสรรนี้ไม่ไล่ตามเป้าเต็มที่ ๆ — เพิ่มเป้า/น้ำหนักความเสี่ยงต้องแลกกับ drawdown ที่สูงขึ้น"
            )

        return PortfolioRecommendation(
            allocation=allocation,
            expected_monthly_return_pct=exp_return,
            expected_drawdown_pct=exp_dd,
            reasoning=reasoning,
        )

    @staticmethod
    def _expected(profile: RiskProfile, allocation: list[AllocationItem]) -> tuple[float, float]:
        cash = next((a.weight_pct for a in allocation if a.asset == "Cash"), 0.0)
        exposure = 100.0 - cash
        exp = {"conservative": (1.0, 4.0), "moderate": (2.5, 8.0), "aggressive": (4.5, 14.0)}[profile.value]
        return round(exp[0] * exposure / 100.0, 2), round(exp[1] * exposure / 100.0, 2)
