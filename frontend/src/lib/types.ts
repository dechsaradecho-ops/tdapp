// Shared types mirroring backend schemas
export type RiskProfile = "conservative" | "moderate" | "aggressive";
export type TradingMode = "auto" | "semi_auto" | "manual";
export type FinalDecision = "TRADE" | "WAIT" | "REDUCE RISK" | "INCREASE CASH";

export interface Scenario {
  label: "best_case" | "normal_case" | "worst_case";
  expected_return_pct: number;
  expected_profit: number;
  expected_drawdown_pct: number;
  note: string;
}

export interface GoalAssessment {
  capital: number;
  target_return_pct: number;
  expected_profit: number;
  probability: "high_probability" | "moderate_probability" | "low_probability";
  scenarios: Scenario[];
  risk_warning: string | null;
  reasoning: string[];
}

export interface AssetOpportunity {
  asset: string;
  score: number;
  band: "low" | "medium" | "high" | "very_high";
  reasons: string[];
}

export interface DbCheckResult {
  table?: string;
  client: "ok" | "unavailable";
  verdict: "pass" | "fail" | "partial";
  insert?: "ok" | "FAIL";
  select?: "ok" | "FAIL";
  delete?: "ok" | "FAIL";
  worker_table?: string;
  worker_insert?: "ok" | "FAIL";
  worker_insert_error?: string;
  worker_insert_hint?: string;
  error?: string;
  insert_hint?: string;
  select_hint?: string;
  token?: string;
}

export interface DbCounts {
  client: "ok" | "unavailable";
  verdict: "ok" | "fail";
  error?: string;
  market_analysis?: number;
  signals?: number;
  news_analysis?: number;
  trades?: number;
  market_analysis_latest?: string;
  signals_latest?: string;
  news_analysis_latest?: string;
  trades_latest?: string;
}

export interface MarketSummary {
  regime: string;
  confidence: number;
  explanation: string;
  sentiment: "bullish" | "bearish" | "neutral";
  opportunities: AssetOpportunity[];
}

export interface AllocationItem {
  asset: string;
  weight_pct: number;
  rationale: string;
}

export interface PortfolioRecommendation {
  allocation: AllocationItem[];
  total_weight_pct: number;
  expected_monthly_return_pct: number;
  expected_drawdown_pct: number;
  reasoning: string[];
}

export interface RiskStatus {
  risk_level: "low" | "medium" | "high" | "critical";
  current_drawdown_pct: number;
  max_drawdown_pct: number;
  daily_loss_pct: number;
  weekly_loss_pct: number;
  monthly_loss_pct: number;
  open_risk_pct: number;
  trading_paused: boolean;
  message: string;
}

export interface SignalProposal {
  asset: string;
  direction: "BUY" | "SELL";
  confidence: number;
  entry: number;
  stop_loss: number;
  take_profit: number;
  expected_rr: number;
  risk_per_trade_pct: number;
  reason: string[];
  recommendation: FinalDecision;
}

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
