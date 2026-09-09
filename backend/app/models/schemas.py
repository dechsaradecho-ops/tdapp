"""Pydantic schemas for API requests/responses (mirrors DB design)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from itertools import combinations
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.integrations import quotes
from app.integrations.quotes import Candle, _adx, _atr_pct, _ema, _rsi, _supertrend_dir


def _adx_windows(candles: list[Candle]) -> float:
    return _adx(candles)


def _atr_pct_windows(candles: list[Candle]) -> float:
    return _atr_pct(candles)


def _supertrend_dir_windows(candles: list[Candle]) -> int:
    return _supertrend_dir(candles)


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


class GoalRealityContext(BaseModel):
    """Live portfolio/market state folded into the goal assessment.

    All fields are optional-safe: when the DB has no data yet (fresh install,
    stats just reset) the engine falls back to the pure envelope math and
    `data_available=False` tells the UI to show "ยังไม่มีข้อมูลการเทรดจริง".
    """
    data_available: bool = False
    pnl_total: float = 0.0            # realized PnL of closed trades (USD)
    win_rate: float = 0.0             # % of closed trades that won
    closed_count: int = 0
    open_positions: int = 0
    market_regime: str = "sideway"    # bull_trend / bear_trend / sideway / ...
    market_sentiment: str = "neutral" # bullish / bearish / neutral
    kill_switch_engaged: bool = False
    kill_triggers: list[str] = Field(default_factory=list)
    trading_paused: bool = False
    pause_reason: str = ""


class GoalAssessment(BaseModel):
    capital: float
    target_return_pct: float
    expected_profit: float
    probability: Probability
    scenarios: list[Scenario]
    risk_warning: Optional[str] = None
    reasoning: list[str]
    # Live portfolio/market state the assessment was adjusted by (None on the
    # pure-theory path — kept for backward compatibility with old clients).
    reality: Optional[GoalRealityContext] = None


# ---------- Market ----------
class AssetOpportunity(BaseModel):
    asset: str
    score: float = Field(ge=0, le=100)
    band: OpportunityBand
    reasons: list[str]
    # Full scoring breakdown (one line per component, \n-joined in the DB
    # column score_reasons) — home Opportunity-Score popup shows HOW the
    # score was computed. Empty when the row predates migration 026.
    score_reasons: list[str] = []


class MarketSummary(BaseModel):
    regime: MarketRegime
    confidence: float
    explanation: str
    sentiment: Literal["bullish", "bearish", "neutral"]
    opportunities: list[AssetOpportunity]
    # Gate thresholds from trading_settings — frontend uses these to badge each
    # symbol's confidence as pass/fail (XAUUSD compares against the gold gate).
    min_confidence: float = 70.0
    min_confidence_gold: Optional[float] = None


# ---------- Signals / Trades ----------
class LimitLevel(BaseModel):
    """One limit-order rung in a laddered entry (แนวรับ 1 ระดับ).

    price    — limit price of this rung
    risk_pct — share of the total trade risk placed at this rung (sums to 100)
    sl / tp  — per-level stop-loss / take-profit (keeps the RR target)
    """
    price: float
    risk_pct: float
    sl: float
    tp: float
    rr: float


class SLTPLevel(BaseModel):
    """One SL/TP distance preview tier (สั้น/กลาง/ยาว) on a signal card.

    The card always previews 3 candidate stop distances; the user's
    sl_distance_mode setting decides which tier actually opens the order.
    """
    label: str                 # สั้น / กลาง / ยาว
    atr_multiple: float        # 1.0 / 1.5 / 2.0 × ATR
    stop_loss: float
    take_profit: float
    rr: float


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
    # laddered entries (แนวรับหลายระดับ) — empty for legacy rows without it
    limit_levels: list[LimitLevel] = []
    # SL/TP distance preview at 3 tiers (สั้น ×1.0 / กลาง ×1.5 / ยาว ×2.0 ATR)
    # — which tier opens the real order is decided by sl_distance_mode below.
    sltp_levels: list[SLTPLevel] = []
    # Effective sl_distance_mode from AppSettings (echoed so the card can
    # highlight the tier that will actually be used for the order).
    sl_distance_mode: Optional[str] = None
    # Present only for rows served from the signals table (tier 1). Live/demo
    # tiers never carry these — the UI uses approved_at to render an
    # "อนุมัติแล้ว เวลา ..." stamp instead of Approve/Reject buttons.
    approval: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    # Read-time note on pending cards that cannot become an order right now
    # because a user limit is hit (open positions / daily / weekly). Signals
    # keep generating all day — limits gate execution, not generation.
    order_blocked: Optional[str] = None
    # CURRENT market price for this card's asset from the intraday spot feed
    # (None when the feed failed). Rendered next to the entry so a stale
    # entry (daily-close anchor) is visible at a glance — the "ราคาเก่า"
    # complaint was entries that LOOKED like live quotes.
    live_price: Optional[float] = None
    # Live-price feed health for the page banner (set once per request, on
    # every card of the response — cards share the same fetch).
    feed_status: Optional[QuoteFeedStatus] = None
    # Pending-only: minutes remaining before this signal leaves the queue and
    # the system re-evaluates (SIGNAL_TTL_MIN = 30). Approved/expired cards
    # omit it (None) — no countdown needed once the fate is decided.
    expires_min_left: Optional[float] = None
    # --- Explainability (2026-09-09): step-by-step Thai calc notes ---
    # e.g. "SL ห่าง 0.0042 (0.36%) → risk $1.00 ด้วย 0.02 lots (FX 100k)",
    # "สเปรด 0.00015 → ต้นทุน $0.15", "RR 1:2 จาก SL/TP". The card renders
    # these in a collapsible "วิธีคำนวณ" block — no bare numbers.
    calc_notes: list[str] = Field(default_factory=list)


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
    # Limits the current values are judged against (2026-09-09: the monitor
    # card showed values without limits so users couldn't tell how close to
    # a breach they were). All default 0.0 so old payloads still validate.
    daily_loss_limit: float = 0.0
    weekly_loss_limit: float = 0.0
    monthly_loss_limit: float = 0.0
    risk_per_trade_pct: float = 0.0
    # Individual breach reasons (same strings joined into message) — the UI
    # renders these as a readable list instead of parsing message.
    breaches: list[str] = Field(default_factory=list)
    # Provenance of open_risk_pct: account-currency amount + equity base +
    # open position count, so the card can show "$X from N trades".
    open_risk_amount: float = 0.0
    equity: float = 0.0
    open_positions: int = 0


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


# =====================================================================
# Extended Trading System — frequency / order / correlation / calendar
# =====================================================================

# ---------- Trade Frequency Engine ----------
class TradeLimits(BaseModel):
    max_trades_daily: int
    max_trades_weekly: int
    max_open_positions: int
    risk_per_trade_pct: float


TRADE_LIMITS_TABLE: dict[RiskProfile, dict[str, float]] = {
    RiskProfile.conservative: {"max_trades_daily": 3, "max_trades_weekly": 15,
                               "max_open_positions": 2, "risk_per_trade_pct": 0.5},
    RiskProfile.moderate: {"max_trades_daily": 6, "max_trades_weekly": 30,
                           "max_open_positions": 4, "risk_per_trade_pct": 1.0},
    RiskProfile.aggressive: {"max_trades_daily": 10, "max_trades_weekly": 50,
                             "max_open_positions": 8, "risk_per_trade_pct": 2.0},
}


class FrequencyDecision(BaseModel):
    allowed: bool
    reason: str
    trades_today: int = 0
    trades_this_week: int = 0
    open_positions: int = 0
    limits: Optional[TradeLimits] = None


GOLD_ASSET = "XAUUSD"

# Contract value per 1.0 lot, used by position sizing (risk_to_lot).
# XAUUSD trades 100 oz per standard lot; FX pairs trade 100,000 units.
# Without the per-asset map, gold sizing used the FX 100k contract and
# produced ~0.00002 lots for a normal risk budget (hidden by the min_lot
# floor) — the position risked a fraction of what the user configured.
CONTRACT_VALUE: dict[str, float] = {"XAUUSD": 100.0}
DEFAULT_CONTRACT_VALUE = 100_000.0


def contract_value_for(asset: str) -> float:
    """Contract value (units per 1.0 lot) for an asset — FX 100k default."""
    return CONTRACT_VALUE.get(str(asset or "").upper(), DEFAULT_CONTRACT_VALUE)


def effective_min_confidence(settings: "AppSettings", asset: str) -> float:
    """Per-asset signal-quality threshold.

    Gold (XAUUSD) uses its own Min Confidence (gold) when the user set one;
    every other asset (and gold without an override) uses the base
    min_confidence. Used by BOTH the scanner (signal generation) and the
    execution gate (order opening) so the two paths never drift apart.
    """
    base = float(settings.min_confidence)
    if str(asset or "").upper() != GOLD_ASSET:
        return base
    gold = settings.min_confidence_gold
    return base if gold is None else float(gold)


def effective_min_lot(settings: "AppSettings", asset: Optional[str]) -> float:
    """Per-asset minimum lot floor.

    Gold (XAUUSD) uses its own Min Lot (gold) when the user set one; every
    other asset (and gold without an override) uses the base min_lot. Used
    by position sizing (execution.size_position) so the floor matches the
    asset actually being ordered.
    """
    base = float(getattr(settings, "min_lot", 0.01) or 0.01)
    if str(asset or "").upper() != GOLD_ASSET:
        return base
    gold = getattr(settings, "min_lot_gold", None)
    return base if gold is None else float(gold)


# ---------- Per-symbol paper spread ----------------------------------------
# Realistic typical spreads (in PRICE UNITS, full bid-ask width) per symbol.
# A single global paper_spread can never fit every asset: 0.00015 is honest
# for EURUSD but absurd for gold (~0.30) and meaningless for USDJPY (~0.015).
# Values are typical retail spreads for the pairs the feeds cover; the
# resolver (effective_spread) prefers a user override, then this table, then
# the legacy global paper_spread.
DEFAULT_SPREADS: dict[str, float] = {
    # Majors
    "EURUSD": 0.00010, "GBPUSD": 0.00015, "USDJPY": 0.015,
    "AUDUSD": 0.00015, "NZDUSD": 0.00020, "USDCAD": 0.00020,
    "USDCHF": 0.00015,
    # EUR crosses
    "EURGBP": 0.00020, "EURJPY": 0.020, "EURAUD": 0.00025,
    "EURNZD": 0.00035, "EURCAD": 0.00025, "EURCHF": 0.00020,
    # GBP crosses
    "GBPJPY": 0.030, "GBPAUD": 0.00035, "GBPNZD": 0.00045,
    "GBPCAD": 0.00035, "GBPCHF": 0.00030,
    # AUD / NZD crosses
    "AUDJPY": 0.025, "AUDNZD": 0.00035, "AUDCAD": 0.00025,
    "AUDCHF": 0.00025, "NZDJPY": 0.025, "NZDCAD": 0.00030,
    # CAD / CHF crosses
    "CADJPY": 0.030, "CADCHF": 0.00030, "CHFJPY": 0.030,
    # Metals (COMEX gold — wide spread, ~3 cents on a ~2400 price)
    "XAUUSD": 0.30,
}


def effective_spread(settings: "AppSettings", asset: Optional[str]) -> float:
    """Per-symbol paper spread (price units) for paper fills.

    Resolution order:
      1. User override (settings.spread_overrides[ASSET]) — set from the
         Settings page per symbol.
      2. Built-in DEFAULT_SPREADS[ASSET] — realistic typical spread.
      3. Legacy global paper_spread (0 = no spread) — covers symbols that
         are not in the table (forward compatibility).
    """
    a = str(asset or "").upper()
    overrides = getattr(settings, "spread_overrides", None) or {}
    if a in overrides and overrides[a] is not None:
        try:
            return max(0.0, float(overrides[a]))
        except (TypeError, ValueError):
            pass
    if a in DEFAULT_SPREADS:
        return float(DEFAULT_SPREADS[a])
    return float(getattr(settings, "paper_spread", 0.0) or 0.0)


class FrequencyEngine:
    """Guards against overtrading — evaluates every order against profile limits."""

    def __init__(self, profile: RiskProfile = RiskProfile.moderate,
                 limits_override: Optional[TradeLimits] = None,
                 min_confidence: float = 70.0,
                 drawdown_throttle_pct: float = 5.0) -> None:
        self.profile = profile
        self._override = limits_override
        self.min_confidence = min_confidence
        self.drawdown_throttle_pct = drawdown_throttle_pct

    def limits(self) -> TradeLimits:
        return self._override or TradeLimits(**TRADE_LIMITS_TABLE[self.profile])

    def evaluate(
        self,
        confidence: float,
        trades_today: int = 0,
        trades_this_week: int = 0,
        open_positions: int = 0,
        current_drawdown_pct: float = 0.0,
        regime: str = "sideway",
        volatility_index: float = 10.0,
    ) -> FrequencyDecision:
        limits = self.limits()
        reason = ""

        # --- Signal quality filter (spec: <70 / <60 => NO TRADE) ---
        if confidence < self.min_confidence:
            reason = (f"Signal quality filter: confidence {confidence:.0f} < "
                      f"{self.min_confidence:.0f} → NO TRADE")
        # --- Hard limits ---
        elif trades_today >= limits.max_trades_daily:
            reason = f"Daily limit reached ({trades_today}/{limits.max_trades_daily})"
        elif trades_this_week >= limits.max_trades_weekly:
            reason = f"Weekly limit reached ({trades_this_week}/{limits.max_trades_weekly})"
        elif open_positions >= limits.max_open_positions:
            reason = f"Max open positions ({open_positions}/{limits.max_open_positions})"
        # --- Regime-based throttling ---
        elif regime in ("sideway", "high_volatility", "news_driven_market"):
            reason = (f"Regime '{regime}' + volatility {volatility_index:.0f} → "
                      f"throttled (limit {max(1, limits.max_trades_daily // 2)})")
        # --- Drawdown-based throttling ---
        elif current_drawdown_pct > self.drawdown_throttle_pct:
            reason = f"Drawdown {current_drawdown_pct:.1f}% → frequency throttled"

        return FrequencyDecision(
            allowed=reason == "",
            reason=reason or "All frequency checks passed",
            trades_today=trades_today,
            trades_this_week=trades_this_week,
            open_positions=open_positions,
            limits=limits,
        )


# ---------- Order Strategy Engine ----------
class OrderType(str, Enum):
    market = "market"
    buy_limit = "buy_limit"
    sell_limit = "sell_limit"
    buy_stop = "buy_stop"
    sell_stop = "sell_stop"


class EntryLeg(BaseModel):
    order_type: OrderType
    price: float
    lot: float
    risk_pct: float
    note: str = ""


class OrderPlan(BaseModel):
    asset: str
    direction: Literal["BUY", "SELL"]
    entries: list[EntryLeg]
    total_lots: float
    total_risk_pct: float
    average_entry: float
    stop_loss: float
    take_profit: float
    rationale: list[str]


class OrderStrategyEngine:
    """Maps a proposal to Market/Limit/Stop orders with multi-entry (max 3) legs."""

    def build_plan(
        self,
        asset: str,
        direction: Literal["BUY", "SELL"],
        entry: float,
        stop_loss: float,
        take_profit: float,
        atr_pct: float = 0.8,
        regime: str = "bull_trend",
        equity: float = 10_000.0,
        risk_per_trade_pct: float = 1.0,
    ) -> OrderPlan:
        """Multi-entry: limit legs into pullbacks for trends, stop legs on breakouts."""
        distance = abs(entry - stop_loss)
        if distance <= 0:
            distance = entry * max(atr_pct, 0.2) / 100.0

        reasons: list[str] = []
        legs: list[EntryLeg] = []

        if regime in ("bull_trend", "strong_bull_trend", "bear_trend", "strong_bear_trend") \
                and atr_pct >= 0.4:
            # Trend: entry#1 market now, #2/#3 limit at pullback levels (0.5 / 1.0 ATR)
            reasons.append(
                f"Regime '{regime}' + ATR {atr_pct:.2f}% → Entry#1 Market, "
                f"#2/#3 Limit ที่ pullback 0.5/1.0 ATR")
            p1 = entry
            p2 = entry - direction_sign(direction) * distance * 0.5
            p3 = entry - direction_sign(direction) * distance * 1.0
            weights = (0.5, 0.3, 0.2)
            legs = [
                EntryLeg(order_type=OrderType.market, price=round(p1, 5),
                         lot=round(risk_to_lot(equity, risk_per_trade_pct, distance) * weights[0], 2),
                         risk_pct=round(risk_per_trade_pct * weights[0], 2), note="ทยอยเข้าทันที"),
                EntryLeg(order_type=OrderType.buy_limit if direction == "BUY" else OrderType.sell_limit,
                         price=round(p2, 5), lot=round(risk_to_lot(equity, risk_per_trade_pct, distance) * weights[1], 2),
                         risk_pct=round(risk_per_trade_pct * weights[1], 2), note="รอดึงกลับ 0.5 × ระยะ SL"),
                EntryLeg(order_type=OrderType.buy_limit if direction == "BUY" else OrderType.sell_limit,
                         price=round(p3, 5), lot=round(risk_to_lot(equity, risk_per_trade_pct, distance) * weights[2], 2),
                         risk_pct=round(risk_per_trade_pct * weights[2], 2), note="รอดึงกลับ 1.0 × ระยะ SL"),
            ]
        else:
            # Sideway/high-vol: breakout stops instead of chasing
            reasons.append(
                f"Regime '{regime}' → รอ breakout: Entry#1 Stop ที่ trigger, "
                f"#2/#3 Stop เพิ่มเมื่อ momentum ยืนยัน")
            p1 = entry + direction_sign(direction) * distance * 0.25
            p2 = entry + direction_sign(direction) * distance * 0.5
            p3 = entry + direction_sign(direction) * distance * 0.75
            weights = (0.4, 0.35, 0.25)
            legs = [
                EntryLeg(order_type=OrderType.buy_stop if direction == "BUY" else OrderType.sell_stop,
                         price=round(p1, 5), lot=round(risk_to_lot(equity, risk_per_trade_pct, distance) * weights[0], 2),
                         risk_pct=round(risk_per_trade_pct * weights[0], 2), note="Stop trigger ใกล้สุด"),
                EntryLeg(order_type=OrderType.buy_stop if direction == "BUY" else OrderType.sell_stop,
                         price=round(p2, 5), lot=round(risk_to_lot(equity, risk_per_trade_pct, distance) * weights[1], 2),
                         risk_pct=round(risk_per_trade_pct * weights[1], 2), note="Stop เพิ่มเมื่อยืนยัน"),
                EntryLeg(order_type=OrderType.buy_stop if direction == "BUY" else OrderType.sell_stop,
                         price=round(p3, 5), lot=round(risk_to_lot(equity, risk_per_trade_pct, distance) * weights[2], 2),
                         risk_pct=round(risk_per_trade_pct * weights[2], 2), note="Stop สุดท้ายยืนยันเทรนด์"),
            ]

        total_lots = round(sum(l.lot for l in legs), 2)
        total_risk = round(sum(l.risk_pct for l in legs), 2)
        avg = round(sum(l.price * l.lot for l in legs) / max(total_lots, 1e-9), 5)
        return OrderPlan(
            asset=asset, direction=direction, entries=legs,
            total_lots=total_lots, total_risk_pct=total_risk, average_entry=avg,
            stop_loss=round(stop_loss, 5), take_profit=round(take_profit, 5),
            rationale=reasons,
        )


def direction_sign(direction: str) -> float:
    return 1.0 if direction == "BUY" else -1.0


def risk_to_lot(equity: float, risk_pct: float, stop_distance: float,
                contract_value: float = 100_000.0) -> float:
    """Risk% → lots. risk = lots × dist × contract_value.

    contract_value defaults to the FX standard lot (100k units). Callers
    sizing gold (XAUUSD) pass 100.0 (100 oz per lot) — see contract_value_for.
    """
    if stop_distance <= 0 or equity <= 0:
        return 0.0
    risk_amount = equity * risk_pct / 100.0
    return max(0.0, round(risk_amount / (stop_distance * contract_value), 2))


def risk_to_lot_for(equity: float, risk_pct: float, stop_distance: float,
                    asset: str) -> float:
    """risk_to_lot with the per-asset contract value (gold = 100 oz/lot).

    XAUUSD SL 50 pts, $100 risk → 100 / (50 × 100) = 0.02 lots (correct);
    the FX-contract version returned 0.00002 → rounded to 0.0 and silently
    hidden by the min_lot floor.
    """
    return risk_to_lot(equity, risk_pct, stop_distance,
                       contract_value=contract_value_for(asset))


# ---------- Correlation & Exposure ----------
class CorrelationMatrix(BaseModel):
    """Pairwise correlation, -1..+1."""
    forex: float = 0.0
    gold: float = 0.0
    crypto: float = 0.0
    indices: float = 0.0
    matrix: dict[str, float] = Field(default_factory=dict)


class ExposureBreakdown(BaseModel):
    currency: str
    exposure_pct: float
    direction_net: Literal["net_long", "net_short", "flat"]


class CorrelationEngine:
    """Static FX-class correlation priors + portfolio-level aggregation."""

    CLASS_CORRELATION = {
        ("forex", "forex"): 0.55,
        ("forex", "gold"): -0.30,
        ("forex", "crypto"): 0.10,
        ("forex", "indices"): 0.35,
        ("gold", "gold"): 1.0,
        ("gold", "crypto"): 0.15,
        ("gold", "indices"): 0.20,
        ("crypto", "crypto"): 1.0,
        ("crypto", "indices"): 0.45,
        ("indices", "indices"): 0.85,
    }

    @staticmethod
    def asset_class(asset: str) -> str:
        # Gold/crypto first (all 6-alpha too), then ANY 6-letter pair is FX —
        # the old endswith("USD") check misclassified USDJPY/EURCHF/GBPCHF
        # as "indices" so CHF/JPY stacks never tripped the correlation cap.
        a = str(asset or "").upper()
        if a in ("XAUUSD", "XAGUSD"):
            return "gold"
        if a in ("BTCUSD", "ETHUSD"):
            return "crypto"
        if len(a) == 6 and a.isalpha():
            return "forex"
        return "indices"

    @classmethod
    def pairwise(cls, a: str, b: str) -> float:
        """Correlation of two assets (converted to their class first)."""
        ca, cb = cls.asset_class(a), cls.asset_class(b)
        return cls.CLASS_CORRELATION.get((ca, cb), cls.CLASS_CORRELATION.get((cb, ca), 0.0))

    @classmethod
    def portfolio_correlation(cls, assets: list[str]) -> float:
        """0-100 — how concentrated the portfolio is in one risk factor."""
        if len(assets) <= 1:
            return 0.0
        vals = [abs(cls.pairwise(a, b)) for a, b in combinations(assets, 2)]
        return round(min(100.0, sum(vals) / len(vals) * 100), 1)


class ExposureEngine:
    """Sums currency exposure across open positions (USD/EUR/JPY/Gold/Crypto)."""

    CURRENCY_MAP = {
        "EURUSD": ["USD", "EUR"], "GBPUSD": ["USD", "GBP"],
        "USDJPY": ["JPY", "USD"], "AUDUSD": ["USD", "AUD"],
        "XAUUSD": ["Gold", "USD"], "BTCUSD": ["Crypto", "USD"],
    }

    @classmethod
    def _currencies_for(cls, asset: str) -> list[str]:
        """Exposure currencies for any asset — explicit map first, then a
        generic FX derivation so Settings-added pairs (EURJPY, GBPCAD, …)
        bucket correctly instead of landing in "Other"."""
        ccy = cls.CURRENCY_MAP.get(asset)
        if ccy:
            return ccy
        parts = quotes.fx_parts(asset)
        if parts:
            return list(parts)
        return ["Other"]

    @classmethod
    def analyze(cls, open_positions: list[dict]) -> list[ExposureBreakdown]:
        """positions: [{"asset", "direction": BUY/SELL, "volume"(lots), "price"}]."""
        notional: dict[str, float] = {}
        for p in open_positions:
            ccy = cls._currencies_for(str(p["asset"]).upper())
            weight = float(p.get("volume", 0)) * float(p.get("price", 1))
            signed = weight if str(p.get("direction", "")).upper() == "BUY" else -weight
            for c in ccy:
                notional[c] = notional.get(c, 0.0) + signed
        total = sum(abs(v) for v in notional.values()) or 1.0
        out: list[ExposureBreakdown] = []
        for ccy, val in sorted(notional.items(), key=lambda kv: -abs(kv[1])):
            pct = abs(val) / total * 100
            out.append(ExposureBreakdown(
                currency=ccy, exposure_pct=round(pct, 1),
                direction_net=("net_long" if val > 0 else "net_short" if val < 0 else "flat")))
        return out


# ---------- Economic Calendar & News Risk ----------
class EconomicEvent(BaseModel):
    event: str
    currency: str = "USD"
    time_utc: Optional[datetime] = None
    impact: Literal["high", "medium", "low"] = "high"
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None


class NewsRiskStatus(BaseModel):
    status: Literal["SAFE", "CAUTION", "DANGER"]
    reason: str
    next_high_impact: Optional[EconomicEvent] = None
    minutes_to_next: Optional[float] = None


ECONOMIC_EVENT_TYPES = [
    "CPI", "Core CPI", "NFP", "FOMC", "GDP",
    "Interest Rate Decision", "Retail Sales", "PMI",
    "Unemployment Rate", "Geopolitical Events",
]


class EconomicCalendarEngine:
    """News-risk gate: blocks new orders within 30 min before high-impact events.

    The live calendar feed is fetched by the news worker; this engine only
    evaluates timing, so it stays testable without network.
    """
    HIGH_IMPACT_BLOCK_MIN = 30.0

    def __init__(self, block_minutes: float = 30.0) -> None:
        self.block_minutes = block_minutes

    def news_risk(
        self,
        events: list[EconomicEvent],
        now: Optional[datetime] = None,
    ) -> NewsRiskStatus:
        now = now or datetime.now(timezone.utc)
        upcoming = [e for e in events
                    if e.time_utc and e.time_utc > now and e.impact == "high"]
        if not upcoming:
            return NewsRiskStatus(status="SAFE",
                                  reason="ไม่มีข่าว impact สูงใกล้ตัวในช่วงเวลานี้")
        upcoming.sort(key=lambda e: e.time_utc)  # type: ignore[arg-type, return-value]
        nxt = upcoming[0]
        minutes = (nxt.time_utc - now).total_seconds() / 60.0  # type: ignore[operator]
        block_min = self.block_minutes
        if minutes < block_min:
            return NewsRiskStatus(
                status="DANGER",
                reason=f"{nxt.event} อีก {minutes:.0f} นาที → ห้ามเปิดออเดอร์ใหม่ "
                       f"(<{block_min:g} นาที)",
                next_high_impact=nxt, minutes_to_next=round(minutes, 1))
        if minutes < block_min * 4:
            return NewsRiskStatus(
                status="CAUTION",
                reason=f"{nxt.event} อีก {minutes:.0f} นาที → เตรียมรับความผันผวน",
                next_high_impact=nxt, minutes_to_next=round(minutes, 1))
        return NewsRiskStatus(
            status="SAFE", reason=f"{nxt.event} ยังอีก {minutes:.0f} นาที → ปลอดภัย",
            next_high_impact=nxt, minutes_to_next=round(minutes, 1))


# ---------- Market Session Engine ----------
def is_market_closed(now: Optional[datetime] = None) -> bool:
    """True when the FX/gold market is closed (weekend).

    FX & gold trade continuously from Sunday 21:00 UTC to Friday 21:00 UTC
    (Friday close rolls into Saturday 00:00 UTC). Shared by the scanner
    (stops generating signals) and the API (so the UI can show a
    "ตลาดปิด" banner instead of stale/demo cards).
    """
    now = now or datetime.now(timezone.utc)
    wd, h = now.weekday(), now.hour
    if wd == 5:                      # Saturday
        return True
    if wd == 6 and h < 21:           # Sunday before 21:00 UTC reopen
        return True
    if wd == 4 and h >= 21:          # Friday after 21:00 UTC close
        return True
    return False


def next_market_open(now: Optional[datetime] = None) -> datetime:
    """Next reopen time (Sunday 21:00 UTC) when the market is closed."""
    now = now or datetime.now(timezone.utc)
    if not is_market_closed(now):
        return now
    # Walk forward to the next Sunday 21:00 UTC.
    candidate = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if now.weekday() == 4 and now.hour >= 21:      # Friday after close
        candidate += timedelta(days=3)             # → Monday 21:00, then walk
    while not (candidate.weekday() == 6 and candidate.hour >= 21):
        candidate += timedelta(days=1)
    return candidate


class MarketSessionStatus(BaseModel):
    active_sessions: list[str]
    overlapping: bool
    volatility_hint: Literal["low", "medium", "high"]
    current_utc_time: str
    # True during the weekend close (Fri 21:00 UTC → Sun 21:00 UTC) — the UI
    # shows a "ตลาดปิด" banner and the scanner stops emitting signals.
    market_closed: bool = False
    # ISO timestamp of the next reopen (present only when market_closed).
    next_open_utc: Optional[datetime] = None


SESSIONS = [
    ("Sydney", 21, 6),   # 21:00-06:00 UTC
    ("Tokyo", 0, 9),
    ("London", 7, 16),
    ("New York", 12, 21),
]


class SessionEngine:
    @staticmethod
    def active(now: Optional[datetime] = None) -> MarketSessionStatus:
        now = now or datetime.now(timezone.utc)
        hour = now.hour
        active: list[str] = []
        for name, start, end in SESSIONS:
            if start < end:
                if start <= hour < end:
                    active.append(name)
            elif hour >= start or hour < end:  # wraps midnight (Sydney)
                active.append(name)
        overlap = len(active) >= 2
        if "London" in active and "New York" in active:
            hint: Literal["low", "medium", "high"] = "high"
        elif overlap or "London" in active or "New York" in active:
            hint = "medium"
        else:
            hint = "low"
        closed = is_market_closed(now)
        return MarketSessionStatus(
            active_sessions=active, overlapping=overlap, volatility_hint=hint,
            current_utc_time=now.strftime("%H:%M UTC"),
            market_closed=closed,
            next_open_utc=next_market_open(now) if closed else None)


# ---------- Kill Switch Engine ----------
class KillSwitchStatus(BaseModel):
    engaged: bool
    triggers: list[str]
    checked: list[str] = Field(default_factory=list)
    message: str = ""


class KillSwitchEngine:
    """Hard stop — any single trigger halts all trading immediately."""

    def __init__(self, daily_loss_limit: float = 2.0,
                 weekly_loss_limit: float = 5.0,
                 monthly_loss_limit: float = 8.0,
                 drawdown_limit: float = 10.0) -> None:
        self.daily_loss_limit = daily_loss_limit
        self.weekly_loss_limit = weekly_loss_limit
        self.monthly_loss_limit = monthly_loss_limit
        self.drawdown_limit = drawdown_limit

    def evaluate(
        self,
        daily_loss_pct: float = 0.0,
        weekly_loss_pct: float = 0.0,
        monthly_loss_pct: float = 0.0,
        drawdown_pct: float = 0.0,
        broker_connected: bool = True,
        market_data_ok: bool = True,
        ai_provider_ok: bool = True,
        execution_ok: bool = True,
    ) -> KillSwitchStatus:
        triggers: list[str] = []
        if daily_loss_pct > self.daily_loss_limit:
            triggers.append(f"Daily loss {daily_loss_pct:.2f}% > {self.daily_loss_limit:g}%")
        if weekly_loss_pct > self.weekly_loss_limit:
            triggers.append(f"Weekly loss {weekly_loss_pct:.2f}% > {self.weekly_loss_limit:g}%")
        if monthly_loss_pct > self.monthly_loss_limit:
            triggers.append(f"Monthly loss {monthly_loss_pct:.2f}% > {self.monthly_loss_limit:g}%")
        if drawdown_pct > self.drawdown_limit:
            triggers.append(f"Drawdown {drawdown_pct:.2f}% > {self.drawdown_limit:g}%")
        if not broker_connected:
            triggers.append("Broker disconnected")
        if not market_data_ok:
            triggers.append("Market data failure")
        if not ai_provider_ok:
            triggers.append("AI provider failure")
        if not execution_ok:
            triggers.append("Execution failure")

        engaged = bool(triggers)
        return KillSwitchStatus(
            engaged=engaged, triggers=triggers,
            checked=["daily_loss", "weekly_loss", "monthly_loss", "drawdown",
                     "broker", "market_data", "ai_provider", "execution"],
            message=("KILL SWITCH ENGAGED — trading halted: " + "; ".join(triggers)
                     if engaged else "All clear — kill switch disengaged"),
        )


# ---------- AI Risk Officer ----------
class RiskOfficerReview(BaseModel):
    verdict: Literal["APPROVED", "REJECTED"]
    rejects: list[str]
    review_notes: list[str]


class RiskOfficer:
    """Final AI authority — can veto any trade/strategy/allocation/target."""

    def review_trade(
        self,
        confidence: float,
        opportunity_score: float,
        frequency: FrequencyDecision,
        news_risk: NewsRiskStatus,
        kill_switch: KillSwitchStatus,
        correlation_score: float = 0.0,
        correlation_cap: float = 80.0,
        order_plan: Optional[OrderPlan] = None,
        min_confidence: float = 70.0,
        min_opportunity: float = 60.0,
    ) -> RiskOfficerReview:
        """Quality thresholds come from the caller's AppSettings (Min
        Confidence — including the gold override via effective_min_confidence —
        and Min Opportunity), NOT hardcoded. The officer is the final veto on
        RISK; it must not re-reject a quality bar the user already lowered.
        Bug (2026-09-04): hardcoded 70 vetoed XAUUSD 65.8% even though the
        user's Min Confidence (gold) allowed the signal through the scanner."""
        rejects: list[str] = []
        notes: list[str] = []
        if confidence < min_confidence:
            rejects.append(
                f"Reject Trade: confidence {confidence:.0f} < {min_confidence:.0f}")
        if opportunity_score < min_opportunity:
            rejects.append(
                f"Reject Trade: opportunity score {opportunity_score:.0f} < {min_opportunity:.0f}")
        if not frequency.allowed:
            rejects.append(f"Reject Trade: {frequency.reason}")
        if news_risk.status == "DANGER":
            rejects.append(f"Reject Strategy: {news_risk.reason}")
        if kill_switch.engaged:
            rejects.append(f"Reject Allocation: {kill_switch.message}")
        if correlation_score > correlation_cap:
            rejects.append(
                f"Reject Allocation: portfolio correlation {correlation_score:.0f} > cap {correlation_cap}")
        if order_plan and order_plan.total_risk_pct > 2.0 and len(order_plan.entries) < 3:
            notes.append("Multi-entry risk concentration — prefer 3-leg plan")
        if rejects:
            notes.insert(0, "Risk Officer ใช้สิทธิ VETO — ไม่อนุมัติรายการนี้")
        return RiskOfficerReview(verdict="REJECTED" if rejects else "APPROVED",
                                 rejects=rejects, review_notes=notes)


# ---------- Trading Journal ----------
class JournalEntry(BaseModel):
    id: Optional[str] = None
    asset: str
    direction: Literal["BUY", "SELL"]
    entry_price: float
    exit_price: Optional[float] = None
    holding_time_min: Optional[float] = None
    pnl: Optional[float] = None
    rr_ratio: Optional[float] = None
    market_regime: str = ""
    opportunity_score: float = 0.0
    ai_explanation: str = ""
    closed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class JournalAnalysis(BaseModel):
    period_days: int
    total_trades: int
    win_rate_pct: float
    profit_factor: float
    average_rr: float
    best_setup: Optional[JournalEntry] = None
    worst_setup: Optional[JournalEntry] = None


def analyze_journal(entries: list[JournalEntry], period_days: int = 30) -> JournalAnalysis:
    closed = [e for e in entries if e.pnl is not None]
    wins = [e for e in closed if e.pnl > 0]
    losses = [e for e in closed if e.pnl <= 0]
    gross_profit = sum(e.pnl for e in wins)
    gross_loss = abs(sum(e.pnl for e in losses))
    best = max(closed, key=lambda e: e.pnl) if closed else None
    worst = min(closed, key=lambda e: e.pnl) if closed else None
    rrs = [e.rr_ratio for e in closed if e.rr_ratio is not None]
    return JournalAnalysis(
        period_days=period_days, total_trades=len(closed),
        win_rate_pct=round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        profit_factor=round(gross_profit / gross_loss, 2) if gross_loss > 0
            else (99.0 if gross_profit > 0 else 0.0),
        average_rr=round(sum(rrs) / len(rrs), 2) if rrs else 0.0,
        best_setup=best, worst_setup=worst,
    )


# ---------- Backtest Center ----------
class BacktestConfig(BaseModel):
    asset: str = "EURUSD"
    indicator: Literal["EMA", "RSI", "MACD", "ADX", "ATR", "SuperTrend", "PriceAction"] = "EMA"
    days: int = Field(120, ge=30, le=365)
    initial_capital: float = 10_000.0
    risk_per_trade_pct: float = 1.0


class BacktestResult(BaseModel):
    config: BacktestConfig
    total_trades: int
    win_rate_pct: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown_pct: float
    final_equity: float
    equity_curve: list[float] = Field(default_factory=list)
    note: str = ""


INDICATORS = ["EMA", "RSI", "MACD", "ADX", "ATR", "SuperTrend", "PriceAction"]


def run_backtest(candles: list[Candle], config: BacktestConfig) -> BacktestResult:
    """Long/flat backtest driven by the chosen indicator over daily closes."""
    closes = [c.c for c in candles]
    if len(closes) < 30:
        return BacktestResult(config=config, total_trades=0, win_rate_pct=0.0,
                              profit_factor=0.0, sharpe_ratio=0.0, max_drawdown_pct=0.0,
                              final_equity=config.initial_capital,
                              note="ข้อมูลเทียนไม่พอ (ต้อง ≥30 แท่ง)")
    equity = config.initial_capital
    start_equity = equity
    position: Optional[int] = None  # 1 long, None flat
    entry_px = 0.0
    trades: list[float] = []
    curve: list[float] = [equity]
    peak = equity
    max_dd = 0.0

    def signal_at(i: int) -> bool:
        c = closes[max(0, i - 20):i + 1]
        if config.indicator == "EMA":
            return closes[i] > _ema(c, 10)[-1]
        if config.indicator == "RSI":
            return _rsi(c) > 50
        if config.indicator == "MACD":
            f, s = _ema(c, 12)[-1], _ema(c, 26)[-1]
            return f > s
        if config.indicator == "ADX":
            return _adx_windows(c) > 25
        if config.indicator == "ATR":
            return _atr_pct_windows(candles[max(0, i - 20):i + 1]) < 1.5
        if config.indicator == "SuperTrend":
            return _supertrend_dir_windows(candles[max(0, i - 20):i + 1]) > 0
        return closes[i] > max(closes[max(0, i - 5):i])  # PriceAction: 5-bar breakout

    for i in range(25, len(closes) - 1):
        sig = signal_at(i)
        if position is None and sig:
            position = 1
            entry_px = closes[i + 1]  # enter next bar open
        elif position == 1 and not sig:
            ret = (closes[i + 1] - entry_px) / entry_px
            equity *= (1 + ret)
            trades.append(ret)
            curve.append(equity)
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak * 100)
            position = None

    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    gp, gl = sum(wins), abs(sum(losses))
    if trades:
        rets = trades
        mean = sum(rets) / len(rets)
        std = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
        sharpe = mean / std * (252 ** 0.5) if std > 0 else 0.0
    else:
        sharpe = 0.0
    return BacktestResult(
        config=config, total_trades=len(trades),
        win_rate_pct=round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        profit_factor=round(gp / gl, 2) if gl > 0 else (99.0 if gp > 0 else 0.0),
        sharpe_ratio=round(sharpe, 2), max_drawdown_pct=round(max_dd, 2),
        final_equity=round(equity, 2), equity_curve=[round(v, 2) for v in curve],
        note=f"indicator={config.indicator}, long/flat บน daily candles (next-bar open execution)")


# ---------- Walk-Forward & Paper Trading ----------
class WalkForwardResult(BaseModel):
    segments: int
    in_sample_win_rates: list[float]
    out_sample_win_rates: list[float]
    reliability_score: float
    note: str = ""


# Walk Forward: every segment must clear run_backtest's 25-bar indicator
# warmup on BOTH sides of the 60/40 IS/OOS split. 70 bars → IS ≈ 42,
# OOS ≈ 28 — both leave room for real signal flips. Below this, windows
# produce 0 trades and the reliability formula degenerates to a constant
# 40 (ratio 0 × 0.6 + consistency 1.0 × 0.4) identical for every asset.
MIN_SEGMENT_BARS = 70


def walk_forward(candles: list[Candle], config: BacktestConfig,
                 segments: int = 4) -> WalkForwardResult:
    """Split history into segments; IS = train, OOS = validation.

    Reliability Score: consistency between IS and OOS performance (0-100).

    Segments shrink adaptively so each one clears the indicator warmup on
    both IS and OOS sides (Bug 2026-09-05: days=120 forced 4 × ~35-bar
    segments → IS windows shorter than the 25-bar warmup → 0 trades in
    every window → constant fake reliability 40 for every asset).
    """
    closes = [c.c for c in candles]
    if len(closes) < MIN_SEGMENT_BARS:
        return WalkForwardResult(
            segments=0, in_sample_win_rates=[], out_sample_win_rates=[],
            reliability_score=0.0,
            note=f"ข้อมูลไม่พอสำหรับ Walk Forward (มี {len(closes)} แท่ง ต้อง ≥ "
                 f"{MIN_SEGMENT_BARS}) — เพิ่มจำนวนวัน (Days) แล้วรันใหม่")
    while segments > 1 and len(closes) // segments < MIN_SEGMENT_BARS:
        segments -= 1
    seg_len = len(closes) // segments
    is_wr: list[float] = []
    oos_wr: list[float] = []
    for s in range(segments):
        seg = candles[s * seg_len:(s + 1) * seg_len]
        # in-sample = first 60% of segment, out-of-sample = last 40%
        split = int(len(seg) * 0.6)
        is_res = run_backtest(seg[:split], config)
        oos_res = run_backtest(seg[split:], config)
        is_wr.append(is_res.win_rate_pct)
        oos_wr.append(oos_res.win_rate_pct)
    avg_is = sum(is_wr) / len(is_wr) if is_wr else 0
    avg_oos = sum(oos_wr) / len(oos_wr) if oos_wr else 0
    # reliability: OOS performance relative to IS, penalized by variance of OOS
    consistency = 1.0 - (max(oos_wr) - min(oos_wr)) / 100.0 if oos_wr else 0.0
    ratio = min(1.0, avg_oos / avg_is) if avg_is > 0 else 0.0
    if avg_is == 0:
        # No in-sample trades at all (e.g. one-directional trend where the
        # signal never flips) — the IS/OOS ratio is undefined; report 0
        # with an honest note instead of a formula-derived fake number.
        reliability = 0.0
        note = (f"ใช้ {segments} segment(s) × {seg_len} แท่ง — ไม่มีเทรดใน In-Sample "
                f"(สัญญาณไม่กลับทางเลยในช่วงนี้) จึงประเมิน reliability ไม่ได้")
    else:
        reliability = round(max(0.0, min(100.0, (ratio * 0.6 + consistency * 0.4) * 100)), 1)
        note = f"ใช้ {segments} segment(s) × {seg_len} แท่ง (IS 60% / OOS 40%)"
        if segments < 4:
            note += " — ข้อมูลสั้น ลดจำนวน segments ให้แต่ละช่วงยาวพอ"
    return WalkForwardResult(segments=segments, in_sample_win_rates=is_wr,
                             out_sample_win_rates=oos_wr, reliability_score=reliability,
                             note=note)


class PaperTradingStatus(BaseModel):
    enabled: bool
    virtual_capital: float
    virtual_pnl: float
    open_virtual_orders: int
    ai_coaching: str
    live_readiness_score: float


def paper_trading_status(broker, virtual_capital: float = 100_000.0) -> PaperTradingStatus:
    """Summarize the PaperBroker state as live-readiness.

    Score: win rate (40%) + discipline (closed trades present, 30%) + PnL (30%).
    Accepts the real PaperBroker (_positions + closed_trades dicts) or any object
    exposing a `.trades` list with `.pnl` attributes (tests / alt brokers).
    """
    if hasattr(broker, "closed_trades"):
        closed = [float(t.get("pnl") or 0) for t in broker.closed_trades]
        open_count = len(getattr(broker, "_positions", {}) or {})
    else:
        closed = [float(t.pnl) for t in broker.trades if t.pnl is not None]
        open_count = len([t for t in broker.trades if t.pnl is None])
    wins = [p for p in closed if p > 0]
    win_rate = len(wins) / len(closed) if closed else 0.0
    pnl = sum(closed)
    pnl_score = max(0.0, min(1.0, 0.5 + pnl / max(virtual_capital, 1) * 5))
    coaching = ("ยังไม่มีสถิติเพียงพอ — เทรดกระดาษอย่างน้อย 10 ไม้เพื่อให้ AI ประเมินได้"
                if len(closed) < 10 else
                "Win rate และ discipline ผ่านเกณฑ์ — โค้ชให้คงวินัยตามแผนเดิม")
    readiness = round(win_rate * 40 + (min(len(closed), 10) / 10) * 30 + pnl_score * 30, 1)
    return PaperTradingStatus(
        enabled=True, virtual_capital=virtual_capital, virtual_pnl=round(pnl, 2),
        open_virtual_orders=open_count,
        ai_coaching=coaching, live_readiness_score=readiness)


# ---------- Auto-Trader: execution gate + pause state ----------
class GateReport(BaseModel):
    """Result of running a candidate order through the full safety pipeline.

    Everything the execution path needs to decide (and to explain itself in
    the UI / LINE messages) in one object:
      allowed      — all gates passed; the order may fire
      size_lots    — risk-based volume from risk_to_lot (0 when blocked)
      rejects      — human-readable failure reasons, in gate order
      pause        — live trading_pause state the gate evaluated against
    """
    allowed: bool
    size_lots: float = 0.0
    rejects: list[str] = Field(default_factory=list)
    pause: "PauseStatus"
    checks: list[str] = Field(default_factory=list)


class PauseStatus(BaseModel):
    """Live state of the manual kill switch (trading_pause row)."""
    paused: bool = False
    reason: str = ""
    paused_at: Optional[datetime] = None


# ---------- Auto-Trader: monitor dashboard ----------
class SmartExitFactorScores(BaseModel):
    """9-factor breakdown of the Smart Exit hold-quality score (0-100 each)."""
    trend_strength: float = 50.0
    momentum: float = 50.0
    volume_proxy: float = 50.0
    market_regime: float = 50.0
    news_risk: float = 50.0
    holding_time: float = 50.0
    volatility: float = 50.0
    opportunity_score: float = 50.0
    risk_exposure: float = 50.0


class SmartExitSignals(BaseModel):
    tp_hit: bool = False
    sl_hit: bool = False
    trailing: bool = False
    reversal: bool = False
    news: bool = False
    time_stop: bool = False


class SmartExitInfo(BaseModel):
    """Per-position Smart Exit analysis attached to the monitor snapshot.

    position_age_days / r_multiple / exit_score / quality mirror the spec's
    EXIT DECISION OUTPUT header (e.g. 4 Days / +1.8R / 42 / Low / Close).
    recommendation is the engine action, final the portfolio-level decision,
    reasoning (1-5 lines) ALWAYS explains why — NO POSITION LEFT BEHIND
    without a reason.
    """
    position_age_days: float = 0.0
    r_multiple: float = 0.0
    exit_score: float = 50.0
    quality: str = "Medium"  # High / Medium / Low
    factors: SmartExitFactorScores = SmartExitFactorScores()
    signals: SmartExitSignals = SmartExitSignals()
    recommendation: str = "HOLD"  # HOLD/MOVE_SL/PARTIAL_25/PARTIAL_50/CLOSE/EMERGENCY_CLOSE
    final: str = "CONTINUE"  # CONTINUE/PROTECT/SCALE_OUT/CLOSE/EMERGENCY_CLOSE
    reasoning: list[str] = Field(default_factory=list)
    trigger: str = ""


class MonitorOpenPosition(BaseModel):
    """One open paper position with a live mark and unrealized PnL."""
    id: str
    ticket: str = ""
    asset: str
    direction: str
    volume: float
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    current_price: float
    unrealized_pnl: float
    source: str = "auto"
    created_at: Optional[datetime] = None
    # SL/TP move tracking (migration 021) — the monitor page badges cells
    # whose value differs from the level the position was opened with and
    # tooltips the details (original → current, when, why).
    initial_stop_loss: Optional[float] = None
    initial_take_profit: Optional[float] = None
    sl_moved_at: Optional[datetime] = None
    sl_move_reason: str = ""
    tp_moved_at: Optional[datetime] = None
    tp_move_reason: str = ""
    # Smart Exit analysis (None when the engine is disabled or unevaluated).
    exit_info: Optional[SmartExitInfo] = None
    # --- Explainability (2026-09-09): why these numbers? ---
    # R-multiple at the current mark, $ risk if SL hits, where the mark
    # came from (live/broker/entry), and step-by-step Thai calc notes so
    # the monitor card can show the math instead of bare numbers.
    r_multiple: float = 0.0
    risk_amount: float = 0.0
    price_source: str = ""  # "live" (spot) | "daily" (daily-close fallback) | "broker" | "entry"
    calc_notes: list[str] = Field(default_factory=list)


class MonitorTrade(BaseModel):
    """One execution-journal row (open, closed or rejected)."""
    id: str
    asset: str
    direction: str
    volume: float
    entry_price: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str
    source: str = "auto"
    ticket: Optional[str] = None
    close_reason: Optional[str] = None
    closed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class MonitorStats(BaseModel):
    trades_today: int = 0
    trades_week: int = 0
    open_positions: int = 0
    closed_count: int = 0
    win_rate: float = 0.0          # % of closed trades that won
    pnl_today: float = 0.0
    pnl_week: float = 0.0
    pnl_total: float = 0.0


# ---------- Live quote feed health (surfaced on monitor + signals pages) ----------
class QuoteFeedStatus(BaseModel):
    """Health of the live-price source, so users see WHY a mark may be stale.

    state="ok"    — feed responded, prices fresh
    state="error" — feed call failed (network/timeout/HTTP) → marks fell back
    to broker/entry prices; failed_assets + message tell the user what broke.
    """
    state: str = "ok"                      # "ok" | "error"
    source: str = ""                       # "exchangerate+yahoo" | "frankfurter" | "twelvedata"
    fetched_at: Optional[datetime] = None  # when marks were last fetched
    failed_assets: list[str] = Field(default_factory=list)
    message: str = ""


class MonitorSnapshot(BaseModel):
    """Everything the /monitor dashboard needs in one response."""
    pause: PauseStatus
    order_mode: str = "auto"
    capital: float = 0.0
    kill: KillSwitchStatus
    # Real Risk Engine evaluation over the same open positions + equity the
    # worker sees (plan A 2026-09-09: replaces the frontend's fake
    # /risk/check call with capital*0.005 open_risk that always read "low").
    # Optional so old cached responses / tests without it still validate.
    risk: Optional[RiskStatus] = None
    stats: MonitorStats
    open_positions: list[MonitorOpenPosition] = Field(default_factory=list)
    recent: list[MonitorTrade] = Field(default_factory=list)
    generated_at: Optional[datetime] = None
    # Live-price feed health — rendered as a banner when the feed fails.
    feed_status: Optional[QuoteFeedStatus] = None
    # Live portfolio value for the home page — computed from DB rows
    # (capital + realized closed PnL + unrealized open PnL), replacing the
    # old manual localStorage numbers so รีเซ็ตสถิติ resets them for real.
    equity: float = 0.0
    pnl: float = 0.0


# ---------- Auth: 6-digit PIN gate ----------
class PinStatus(BaseModel):
    """Public auth state — lets the frontend pick setup vs login."""
    pin_set: bool = False
    locked: bool = False
    locked_until: Optional[datetime] = None
    failed_attempts: int = 0
    max_failed: int = 5
    lock_minutes: int = 15


class PinLoginRequest(BaseModel):
    pin: str


class PinSetRequest(BaseModel):
    pin: str


class PinLoginResponse(BaseModel):
    ok: bool = False
    token: Optional[str] = None
    message: str = ""
    remaining_attempts: Optional[int] = None
    locked_until: Optional[datetime] = None


# ---------- Extended Output Format ----------
class ExtendedAnalysis(BaseModel):
    """The 11-section extended output format (spec)."""
    news_calendar: str
    session_analysis: str
    correlation_analysis: str
    order_strategy: str
    execution_plan: str
    risk_officer_review: str
    journal_insight: str
    backtest_result: str
    paper_trading_status: str
    kill_switch_status: str
    final_decision: str

# ---------- App Settings (user-configurable) ----------
class AppSettings(BaseModel):
    """Single global configuration row — editable from the Settings page.

    Field defaults match the shipped engine defaults so that an empty
    `app_settings` table behaves byte-identically to the pre-settings system.
    """
    risk_profile: RiskProfile = RiskProfile.moderate
    capital: float = 10_000.0
    min_confidence: float = 70.0
    # Per-asset override for gold (XAUUSD) — applied to BOTH signal generation
    # (market_scanner) and order opening (execution gate). Falls back to
    # min_confidence when unset (None) so old settings rows keep working.
    min_confidence_gold: Optional[float] = None
    min_opportunity: float = 60.0

    max_trades_daily: int = 6
    max_trades_weekly: int = 30
    max_open_positions: int = 4
    risk_per_trade_pct: float = 1.0
    # Minimum lot size for every opened order (PaperBroker floor). The
    # risk_to_lot result is raised to this value so tiny accounts still trade
    # a visible size — user-configurable (e.g. 0.02) from the Settings page.
    min_lot: float = 0.01
    # Per-asset override for gold (XAUUSD) — applied to position sizing
    # (execution.size_position). Falls back to min_lot when unset (None) so
    # old settings rows keep working.
    min_lot_gold: Optional[float] = None

    # ---- Position management (position_guard) -----------------------------
    # Breakeven: once profit ≥ breakeven_trigger_r × |entry−SL|, move the SL
    # to entry (risk-free trade). 0 disables the feature.
    breakeven_trigger_r: float = 1.0
    # Trailing stop: after breakeven, trail the SL at trail_atr_mult × ATR
    # behind the live price. 0 disables trailing (breakeven only).
    trail_atr_mult: float = 2.0
    # Partial close (TP1): close partial_close_pct of the volume when profit
    # reaches partial_trigger_r × R, then trail the remainder. 0 disables.
    partial_close_pct: float = 0.0
    partial_trigger_r: float = 1.0
    # Time stop: close any position older than max_hold_days days regardless
    # of PnL — stale trend-chase entries decay instead of recovering. 0
    # disables the feature.
    max_hold_days: int = 5
    # Reward:Risk target for every new signal — TP = SL distance × rr_target
    # (1:2 default). Flows into build_proposal → TP, limit ladder and the
    # 3-tier SL/TP preview on the card. Clamped ≥ 0.5 so a typo can't create
    # a TP inside the SL.
    rr_target: float = 2.0

    # ---- Smart Exit Engine (continuous exit evaluation, worker #6) --------
    # Every open position passes evaluate_exit() each guard cycle. All fields
    # carry defaults so an empty trading_settings row behaves identically.
    # Master switch — False restores the legacy guard (breakeven/trailing/
    # partial/time-stop only, no AI exit actions).
    smart_exit_enabled: bool = True
    # Hold-quality score below which the engine closes (or scales out when
    # still profitable). Spec example: 42/Low → Close.
    exit_score_close: float = 45.0
    # Profit Protection: profit ≥ this R with quality != High → scale out
    # 50% so a winner can't fully evaporate. 0 disables.
    profit_protect_r: float = 2.0
    # Trend Reversal: opportunity score below this counts as a reversal vote
    # (2+ of 4 votes → CLOSE). Spec: "Opportunity Score Drops Below 50".
    reversal_opp_min: float = 50.0
    # News Exit: DANGER calendar + profit ≥ news_exit_min_r → CLOSE early.
    news_exit_enabled: bool = True
    news_exit_min_r: float = 1.0
    # Volatility Exit: ATR% above this + profit → scale out (25/50%).
    volatility_exit_atr: float = 2.5
    # NO POSITION LEFT BEHIND: profit < no_behind_min_r AND age >
    # no_behind_hold_mult × avg holding time → auto CLOSE (Capital Efficiency).
    no_behind_min_r: float = 0.5
    no_behind_hold_mult: float = 5.0
    # R-ladder trailing: 1R→BE / 2R→+1R / 3R→+2R. True = ladder (spec),
    # False = legacy single breakeven_trigger_r + trail_atr_mult behaviour.
    trailing_ladder: bool = True

    # ---- Strategy D — gold breakout-retest gate ----------------------------
    # Gold's high ATR makes narrow pullback entries unattractive: in prod,
    # XAUUSD scored 65+ every day in a bull market and chase entries lost.
    # When True, XAUUSD only emits signals on an active breakout (close above
    # the prior 20-bar high) or a successful retest of it, and the SL anchors
    # at the broken level with a 0.5×ATR invalidation buffer. False = old
    # behaviour (gold trades like every other asset).
    gold_breakout_only: bool = True

    # ---- Paper execution realism ------------------------------------------
    # Simulated spread (in price units) applied to paper fills: BUYs fill at
    # entry + spread/2, SELLs at entry − spread/2, so paper PnL reflects the
    # cost a real account would pay. 0 = fill exactly at the mid price.
    # Legacy global fallback — per-symbol spreads (spread_overrides +
    # DEFAULT_SPREADS via effective_spread) take precedence for known assets.
    paper_spread: float = 0.0
    # Per-symbol spread overrides (asset → spread in price units), e.g.
    # {"XAUUSD": 0.35}. Symbols without an entry use the built-in
    # DEFAULT_SPREADS table; unknown symbols fall back to paper_spread.
    # None/{} → built-in defaults only (explicit null clears the override).
    spread_overrides: Optional[dict[str, float]] = None

    max_drawdown_pct: float = 10.0
    kill_daily_loss_pct: float = 2.0
    kill_weekly_loss_pct: float = 5.0
    kill_monthly_loss_pct: float = 8.0
    drawdown_throttle_pct: float = 5.0

    # int to match trading_settings integer columns (float JSON like 30.0 fails Postgres int cast)
    news_block_minutes: int = 30
    news_caution_minutes: int = 120
    correlation_cap: float = 80.0
    order_mode: str = "auto"
    # Which SL/TP distance tier opens the real order. Signal cards preview 3
    # tiers (สั้น ×1.0 / กลาง ×1.5 / ยาว ×2.0 ATR); the stored signal row
    # carries กลาง prices, execute_signal re-derives SL/TP for this tier.
    sl_distance_mode: Literal["short", "medium", "long"] = "medium"
    # ---- SL distance clamp (equal risk distance per asset) -----------------
    # Keep the SL inside a % of price band so every asset risks a similar
    # distance. Close-only FX feeds (Frankfurter) have no intraday wicks →
    # ATR understated → SL too tight, while OHLC feeds (gold) don't — SLs
    # drifted 0.62%→1.17% across pairs in prod. 0 disables a bound; set BOTH
    # to the same value (e.g. 0.8/0.8) to force a fixed SL distance.
    sl_distance_min_pct: float = 0.0
    sl_distance_max_pct: float = 0.0
    default_equity: float = 10_000.0
    paper_virtual_capital: float = 100_000.0

    backtest_days: int = 120
    backtest_indicator: str = "EMA"
    backtest_asset: str = "EURUSD"

    # ---- UI preferences (moved out of localStorage 2026-09-06) -------------
    # Auto-refresh intervals (seconds) for the monitor / signals pages.
    # 0 = auto-refresh off. Stored in trading_settings so the choice follows
    # the account across devices instead of living in one browser.
    monitor_refresh_sec: int = 10
    signals_refresh_sec: int = 0

    # ---- LINE notification categories (per-category on/off) ----------------
    # Each boolean gates one category of LINE push. NotificationService.notify
    # and notification_worker.dispatch_pending both check these before queueing
    # or sending, so a disabled category produces no queue row and no push.
    # All default True — existing rows keep current behaviour.
    notify_trade_opened: bool = True
    notify_trade_closed: bool = True
    notify_stop_loss: bool = True
    notify_risk_warning: bool = True
    notify_daily_digest: bool = True
    notify_daily_summary: bool = True

    # ---- Tradable universe (Settings page) ---------------------------------
    # Assets the user wants the scanner to analyse and the platform to trade.
    # The Settings UI only offers pairs from quotes.SUPPORTED_ASSETS (pairs
    # the price feeds actually cover). Empty/None → DEFAULT_ASSETS so old
    # rows keep the original 5-asset behaviour.
    allowed_assets: list[str] = Field(
        default_factory=lambda: list(quotes.DEFAULT_ASSETS))

    def effective_assets(self) -> list[str]:
        """Sanitized tradable list — validated against SUPPORTED_ASSETS.

        Drops unknown/duplicate entries; falls back to DEFAULT_ASSETS when
        nothing valid remains (e.g. a hand-edited DB row).
        """
        seen: set[str] = set()
        out: list[str] = []
        for a in self.allowed_assets or []:
            u = str(a).upper()
            if u in quotes.SUPPORTED_ASSETS and u not in seen:
                seen.add(u)
                out.append(u)
        return out or list(quotes.DEFAULT_ASSETS)


class SettingsSaveResult(BaseModel):
    ok: bool
    settings: AppSettings
    message: str = ""
