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

// ---------- Quote API call log (7-day auto-expiry) ----------
export interface QuoteApiLog {
  id: string;
  created_at: string;
  asset: string;
  category: "forex" | "gold";
  provider: string;
  url: string;
  api_key_hint: string | null;
  status: "success" | "error";
  http_status: number | null;
  price: number | null;
  error: string | null;
  duration_ms: number | null;
}

export interface QuoteLogBucket {
  total: number;
  success: number;
  error: number;
}

export interface QuoteLogSummary {
  total: number;
  success: number;
  error: number;
  forex: QuoteLogBucket;
  gold: QuoteLogBucket;
  by_provider: Record<string, QuoteLogBucket>;
}

export interface QuoteLogsResponse {
  client: "ok" | "unavailable";
  verdict: "ok" | "fail";
  error?: string;
  logs: QuoteApiLog[];
  summary: QuoteLogSummary;
  ttl_days: number;
}

export interface QuoteTestResult {
  verdict: "ok" | "fail";
  prices: Record<string, number>;
  failures: Record<string, string>;
  tested_at?: string;
  hint?: string;
  error?: string;
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

export interface LimitLevel {
  price: number;
  risk_pct: number;
  sl: number;
  tp: number;
  rr: number;
}

// Health of the live-price source — backend reports failures explicitly so
// the UI can warn "ราคาอาจไม่อัปเดต" instead of silently showing stale marks.
export interface QuoteFeedStatus {
  state: "ok" | "error";
  source: string;
  fetched_at: string | null;
  failed_assets: string[];
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
  limit_levels: LimitLevel[];
  // Present only for DB-backed signals (tier 1) — live/demo tiers omit them.
  // approval === "approved" renders an approval stamp instead of buttons.
  approval?: string | null;
  approved_at?: string | null;
  created_at?: string | null;
  // Set on pending cards that cannot become an order right now because a
  // user limit is hit (open positions / daily / weekly) — the card shows the
  // reason instead of the "ระบบจะยิงออเดอร์เอง" note.
  order_blocked?: string | null;
  // CURRENT market price for this card's asset (intraday spot feed) — lets
  // the card show live price next to entry so a stale entry is obvious.
  live_price?: number | null;
  feed_status?: QuoteFeedStatus | null;
}

// ---------- Extended Trading System ----------
export interface FrequencyDecision {
  allowed: boolean;
  reason: string;
  trades_today: number;
  trades_this_week: number;
  open_positions: number;
  limits: {
    max_trades_daily: number;
    max_trades_weekly: number;
    max_open_positions: number;
    risk_per_trade_pct: number;
  } | null;
}

export interface EntryLeg {
  order_type: "market" | "buy_limit" | "sell_limit" | "buy_stop" | "sell_stop";
  price: number;
  lot: number;
  risk_pct: number;
  note: string;
}

export interface OrderPlan {
  asset: string;
  direction: "BUY" | "SELL";
  entries: EntryLeg[];
  total_lots: number;
  total_risk_pct: number;
  average_entry: number;
  stop_loss: number;
  take_profit: number;
  rationale: string[];
}

export interface CorrelationResponse {
  assets: string[];
  portfolio_correlation: number;
  exposure: { currency: string; exposure_pct: number; direction_net: string }[];
}

export interface NewsRisk {
  status: "SAFE" | "CAUTION" | "DANGER";
  reason: string;
  minutes_to_next: number | null;
}

export interface SessionStatus {
  active_sessions: string[];
  overlapping: boolean;
  volatility_hint: "low" | "medium" | "high";
  current_utc_time: string;
}

export interface KillSwitch {
  engaged: boolean;
  triggers: string[];
  checked: string[];
  message: string;
}

export interface JournalAnalysis {
  period_days: number;
  total_trades: number;
  win_rate_pct: number;
  profit_factor: number;
  average_rr: number;
}

export interface PaperTrading {
  enabled: boolean;
  virtual_capital: number;
  virtual_pnl: number;
  open_virtual_orders: number;
  ai_coaching: string;
  live_readiness_score: number;
}

export interface BacktestConfig {
  asset: string;
  indicator: "EMA" | "RSI" | "MACD" | "ADX" | "ATR" | "SuperTrend" | "PriceAction";
  days: number;
  initial_capital: number;
  risk_per_trade_pct: number;
}

export interface BacktestResult {
  config: BacktestConfig;
  total_trades: number;
  win_rate_pct: number;
  profit_factor: number;
  sharpe_ratio: number;
  max_drawdown_pct: number;
  final_equity: number;
  note: string;
}

export interface WalkForwardResult {
  segments: number;
  in_sample_win_rates: number[];
  out_sample_win_rates: number[];
  reliability_score: number;
  note?: string;
}

export interface ExtendedAnalysis {
  news_calendar: string;
  session_analysis: string;
  correlation_analysis: string;
  order_strategy: string;
  execution_plan: string;
  risk_officer_review: string;
  journal_insight: string;
  backtest_result: string;
  paper_trading_status: string;
  kill_switch_status: string;
  final_decision: string;
}

// ---------- App Settings ----------
export interface AppSettings {
  risk_profile: RiskProfile;
  capital: number;
  min_confidence: number;
  min_opportunity: number;
  max_trades_daily: number;
  max_trades_weekly: number;
  max_open_positions: number;
  risk_per_trade_pct: number;
  max_drawdown_pct: number;
  kill_daily_loss_pct: number;
  kill_weekly_loss_pct: number;
  kill_monthly_loss_pct: number;
  drawdown_throttle_pct: number;
  news_block_minutes: number;
  news_caution_minutes: number;
  correlation_cap: number;
  order_mode: string;
  default_equity: number;
  paper_virtual_capital: number;
  backtest_days: number;
  backtest_indicator: string;
  backtest_asset: string;
}

export interface SettingsSaveResult {
  ok: boolean;
  settings: AppSettings;
  message: string;
}

/** Shared trading kill-switch state (read by auto trader + /approve). */
export interface PauseStatus {
  paused: boolean;
  reason: string;
  paused_at: string | null;
}

/** One open paper position with a live mark and unrealized PnL. */
export interface MonitorOpenPosition {
  id: string;
  ticket: string;
  asset: string;
  direction: string;
  volume: number;
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  current_price: number;
  unrealized_pnl: number;
  source: string;
  created_at: string | null;
}

/** One execution-journal row (open, closed or rejected). */
export interface MonitorTrade {
  id: string;
  asset: string;
  direction: string;
  volume: number;
  entry_price: number;
  exit_price: number | null;
  pnl: number | null;
  status: string;
  source: string;
  ticket: string | null;
  close_reason: string | null;
  closed_at: string | null;
  created_at: string | null;
}

export interface MonitorStats {
  trades_today: number;
  trades_week: number;
  open_positions: number;
  closed_count: number;
  win_rate: number;
  pnl_today: number;
  pnl_week: number;
  pnl_total: number;
}

export interface MonitorSnapshot {
  pause: PauseStatus;
  order_mode: string;
  capital: number;
  kill: KillSwitch;
  stats: MonitorStats;
  open_positions: MonitorOpenPosition[];
  recent: MonitorTrade[];
  generated_at: string | null;
  feed_status?: QuoteFeedStatus | null;
}

/** Response of POST /api/trading/positions/close (manual close popup). */
export interface ClosePositionResult {
  ok: boolean;
  ticket: string;
  asset: string;
  direction: string;
  volume: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  holding_time_min: number | null;
  close_reason: string;
  message: string;
  remaining_open: number;
  total_realized_pnl: number;
  pnl_today: number;
  wins: number;
  losses: number;
  trade_id: string;
  warnings: string[];
}

// ---------- Auth: 6-digit PIN gate ----------
export interface PinStatus {
  pin_set: boolean;
  locked: boolean;
  locked_until: string | null;
  failed_attempts: number;
  max_failed: number;
  lock_minutes: number;
}

export interface PinLoginResponse {
  ok: boolean;
  token: string | null;
  message: string;
  remaining_attempts: number | null;
  locked_until: string | null;
}

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
