"""Pydantic schemas for API requests/responses (mirrors DB design)."""
from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class RiskProfile(str, Enum):
    conservative = "conservative"
    moderate = "moderate"
    aggressive = "aggressive"


class TradingMode(str, Enum):
    auto = "auto"
    semi_auto = "semi_auto"
    manual = "manual"


class MarketRegime(str, Enum):
    strong_bull_trend = "strong_bull_trend"
    bull_trend = "bull_trend"
    sideway = "sideway"
    high_volatility = "high_volatility"
    bear_trend = "bear_trend"
    strong_bear_trend = "strong_bear_trend"
    news_driven_market = "news_driven_market"


class Probability(str, Enum):
    high = "high_probability"
    moderate = "moderate_probability"
    low = "low_probability"


class OpportunityBand(str, Enum):
    low = "low"            # 0-30
    medium = "medium"      # 31-60
    high = "high"          # 61-80
    very_high = "very_high"  # 81-100


class FinalDecision(str, Enum):
    trade = "TRADE"
    wait = "WAIT"
    reduce_risk = "REDUCE RISK"
    increase_cash = "INCREASE CASH"


# ---------- Goal Engine ----------
class GoalInput(BaseModel):
    capital: float = Field(gt=0, description="Starting capital, e.g. 100000 THB")
    target_return_pct: float = Field(gt=0, le=100, description="Monthly target return %, e.g. 3")
    risk_profile: RiskProfile = RiskProfile.moderate
    max_drawdown_pct: float = Field(gt=0, le=100, description="Max allowed drawdown %")
    trading_mode: TradingMode = TradingMode.manual


class Scenario(BaseModel):
    label: Literal["best_case", "normal_case", "worst_case"]
    expected_return_pct: float
    expected_profit: float
    expected_drawdown_pct: float
    note: str


class GoalAssessment(BaseModel):
    capital: float
    target_return_pct: float
    expected_profit: float
    probability: Probability
    scenarios: list[Scenario]
    risk_warning: Optional[str] = None
    reasoning: list[str]


# ---------- Market ----------
class AssetOpportunity(BaseModel):
    asset: str
    score: float = Field(ge=0, le=100)
    band: OpportunityBand
    reasons: list[str]


class MarketSummary(BaseModel):
    regime: MarketRegime
    confidence: float
    explanation: str
    sentiment: Literal["bullish", "bearish", "neutral"]
    opportunities: list[AssetOpportunity]


# ---------- Signals / Trades ----------
class SignalProposal(BaseModel):
    asset: str
    direction: Literal["BUY", "SELL"]
    confidence: float
    entry: float
    stop_loss: float
    take_profit: float
    expected_rr: float
    risk_per_trade_pct: float
    reason: list[str]
    recommendation: FinalDecision


class TradeRecord(BaseModel):
    id: str
    user_id: str
    asset: str
    direction: str
    volume: float
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    pnl: Optional[float] = None
    status: str
    created_at: datetime


# ---------- Portfolio ----------
class PortfolioInput(BaseModel):
    capital: float = Field(gt=0)
    target_return_pct: float = Field(gt=0, le=100)
    max_drawdown_pct: float = Field(gt=0, le=100)
    risk_profile: RiskProfile


class AllocationItem(BaseModel):
    asset: str
    weight_pct: float
    rationale: str


class PortfolioRecommendation(BaseModel):
    allocation: list[AllocationItem]
    total_weight_pct: float = 100.0
    expected_monthly_return_pct: float
    expected_drawdown_pct: float
    reasoning: list[str]


# ---------- Risk ----------
class RiskStatus(BaseModel):
    risk_level: Literal["low", "medium", "high", "critical"]
    current_drawdown_pct: float
    max_drawdown_pct: float
    daily_loss_pct: float
    weekly_loss_pct: float
    monthly_loss_pct: float
    open_risk_pct: float
    trading_paused: bool
    message: str


# ---------- Chat ----------
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    reply: str
    grounded_on: list[str] = Field(
        description="Context keys used: market_condition / risk_analysis / opportunity_score / portfolio_status"
    )
