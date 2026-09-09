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

/** Live portfolio/market state the goal assessment was adjusted by. */
export interface GoalRealityContext {
  data_available: boolean;
  pnl_total: number;
  win_rate: number;
  closed_count: number;
  open_positions: number;
  market_regime: string;
  market_sentiment: string;
  kill_switch_engaged: boolean;
  kill_triggers: string[];
  trading_paused: boolean;
  pause_reason: string;
}

export interface GoalAssessment {
  capital: number;
  target_return_pct: number;
  expected_profit: number;
  probability: "high_probability" | "moderate_probability" | "low_probability";
  scenarios: Scenario[];
  risk_warning: string | null;
  reasoning: string[];
  /** Present when the backend folded real trading state into the result. */
  reality?: GoalRealityContext | null;
}

export interface AssetOpportunity {
  asset: string;
  score: number;
  band: "low" | "medium" | "high" | "very_high";
  reasons: string[];
  /** Full scoring breakdown (ทุก component) — ใช้ใน popup รายละเอียดคะแนน */
  score_reasons?: string[];
}

export interface MarketSummary {
  regime: string;
  confidence: number;
  explanation: string;
  sentiment: "bullish" | "bearish" | "neutral";
  opportunities: AssetOpportunity[];
  min_confidence?: number;
  min_confidence_gold?: number | null;
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

// ---------- Signal lifecycle log (7-day retention) ----------
export interface SignalLog {
  id: string;
  created_at: string | null;
  signal_id: string | null;
  asset: string;
  direction: string;
  event: "created" | "order_opened" | "order_blocked" | "rejected"
    | "expired" | "closed";
  confidence: number | null;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  source: string;
  reason: string;
  ticket: string | null;
  volume: number | null;
  pnl: number | null;
  exit_price: number | null;
}

export interface SignalLogSummary {
  total: number;
  by_event: Record<string, number>;
  by_asset: Record<string, number>;
  opened: number;
  blocked: number;
  expired: number;
  rejected: number;
  closed: number;
}

export interface SignalLogsResponse {
  client: "ok" | "unavailable";
  verdict: "ok" | "fail";
  error?: string;
  logs: SignalLog[];
  summary: SignalLogSummary;
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
  /** Limits the values are judged against (0 = unknown/legacy payload). */
  daily_loss_limit: number;
  weekly_loss_limit: number;
  monthly_loss_limit: number;
  risk_per_trade_pct: number;
  /** Individual breach reasons (joined into message). */
  breaches: string[];
  /** Provenance of open_risk_pct. */
  open_risk_amount: number;
  equity: number;
  open_positions: number;
}

export interface LimitLevel {
  price: number;
  risk_pct: number;
  sl: number;
  tp: number;
  rr: number;
}

// One SL/TP distance tier (สั้น ×1.0 / กลาง ×1.5 / ยาว ×2.0 ATR) previewed on
// the signal card; sl_distance_mode (Settings) picks the tier that opens the order.
export interface SLTPLevel {
  label: string;
  atr_multiple: number;
  stop_loss: number;
  take_profit: number;
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
  // SL/TP preview 3 ระดับระยะ (สั้น/กลาง/ยาว) — empty on legacy rows
  sltp_levels?: SLTPLevel[] | null;
  // Effective sl_distance_mode from settings — the highlighted tier is the
  // one that will actually be used when the order opens.
  sl_distance_mode?: "short" | "medium" | "long" | null;
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
  // Pending-only: นาทีที่เหลือก่อนหมดอายุและระบบเริ่มประเมินใหม่ (TTL 30 นาที)
  expires_min_left?: number | null;
  // Explainability: ขั้นตอนคำนวณทีละขั้น (SL จาก ATR, TP จาก RR, ขนาดไม้,
  // สเปรด) — การ์ดแสดงในบล็อก "วิธีคำนวณ" แบบพับได้
  calc_notes?: string[] | null;
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
  /** True during the weekend close (Fri 21:00 UTC → Sun 21:00 UTC). */
  market_closed?: boolean;
  /** ISO timestamp of the next reopen (present only when market_closed). */
  next_open_utc?: string | null;
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
  /** Gold (XAUUSD) override — null/undefined = use min_confidence */
  min_confidence_gold: number | null;
  min_opportunity: number;
  max_trades_daily: number;
  max_trades_weekly: number;
  max_open_positions: number;
  risk_per_trade_pct: number;
  /** Minimum lot size for opened orders (floor of risk_to_lot sizing) */
  min_lot: number;
  /** Gold (XAUUSD) override — null/undefined = use min_lot */
  min_lot_gold: number | null;
  /** Move SL to entry once profit ≥ breakeven_trigger_r × R (0 = off) */
  breakeven_trigger_r: number;
  /** Trailing stop distance in ATR multiples (0 = off) */
  trail_atr_mult: number;
  /** Close this % of volume at partial_trigger_r × R (0 = off) */
  partial_close_pct: number;
  /** R-multiple that triggers the partial close */
  partial_trigger_r: number;
  /** Time stop: close positions older than this many days (0 = off) */
  max_hold_days: number;
  /** Reward:Risk target — TP = SL distance × rr_target (1:2 default) */
  rr_target: number;
  /** Smart Exit Engine — continuous AI exit evaluation (false = legacy guard only) */
  smart_exit_enabled: boolean;
  /** Hold-quality score below which the engine closes/scales out */
  exit_score_close: number;
  /** Profit ≥ this R with quality != High → scale out 50% (0 = off) */
  profit_protect_r: number;
  /** Opportunity score below this counts as a reversal vote */
  reversal_opp_min: number;
  /** News Exit master switch (DANGER + profit → close early) */
  news_exit_enabled: boolean;
  news_exit_min_r: number;
  /** ATR% above this + profit → scale out (0 = off) */
  volatility_exit_atr: number;
  /** NO POSITION LEFT BEHIND: profit < this R + age > mult × avg → close */
  no_behind_min_r: number;
  no_behind_hold_mult: number;
  /** R-ladder trailing 1R→BE / 2R→+1R / 3R→+2R (false = legacy breakeven+trail) */
  trailing_ladder: boolean;
  /** Strategy D: XAUUSD only trades breakout/retest setups (false = old behaviour) */
  gold_breakout_only: boolean;
  /** Simulated spread (price units) applied to paper fills — legacy global
   *  fallback; per-symbol spreads take precedence for known assets */
  paper_spread: number;
  /** Per-symbol spread overrides (asset → spread in price units) — null =
   *  built-in realistic defaults; symbols without an entry use defaults */
  spread_overrides: Record<string, number> | null;
  max_drawdown_pct: number;
  kill_daily_loss_pct: number;
  kill_weekly_loss_pct: number;
  kill_monthly_loss_pct: number;
  drawdown_throttle_pct: number;
  news_block_minutes: number;
  news_caution_minutes: number;
  correlation_cap: number;
  order_mode: string;
  sl_distance_mode: "short" | "medium" | "long";
  /** SL distance clamp (% of price) — 0 disables a bound; min=max forces a fixed distance */
  sl_distance_min_pct: number;
  sl_distance_max_pct: number;
  default_equity: number;
  paper_virtual_capital: number;
  backtest_days: number;
  backtest_indicator: string;
  backtest_asset: string;
  /** Auto-refresh interval (seconds) for the monitor page — 0 = off (DB-stored) */
  monitor_refresh_sec: number;
  /** Auto-refresh interval (seconds) for the signals page — 0 = off (DB-stored) */
  signals_refresh_sec: number;
  /** LINE notification categories — per-category on/off (default true) */
  notify_trade_opened: boolean;
  notify_trade_closed: boolean;
  notify_stop_loss: boolean;
  notify_risk_warning: boolean;
  notify_daily_digest: boolean;
  notify_daily_summary: boolean;
  /** Tradable universe — scanner analyses + platform trades these pairs only */
  allowed_assets: string[];
}

/** Pairs the price feeds cover (mirror of backend quotes.SUPPORTED_ASSETS).
 *  Only these are offered in the Settings add-pair dropdown. */
export const SUPPORTED_ASSETS = [
  "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF",
  "EURGBP", "EURJPY", "EURAUD", "EURNZD", "EURCAD", "EURCHF",
  "GBPJPY", "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF",
  "AUDJPY", "AUDNZD", "AUDCAD", "AUDCHF", "NZDJPY", "NZDCAD",
  "CADJPY", "CADCHF", "CHFJPY",
  "XAUUSD",
] as const;

export const DEFAULT_ASSETS: string[] = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"];

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

/** 9-factor Smart Exit breakdown (0-100 each, higher = safer to hold). */
export interface SmartExitFactorScores {
  trend_strength: number;
  momentum: number;
  volume_proxy: number;
  market_regime: number;
  news_risk: number;
  holding_time: number;
  volatility: number;
  opportunity_score: number;
  risk_exposure: number;
}

export interface SmartExitSignals {
  tp_hit: boolean;
  sl_hit: boolean;
  trailing: boolean;
  reversal: boolean;
  news: boolean;
  time_stop: boolean;
}

/** Per-position Smart Exit analysis (null when disabled/unevaluated). */
export interface SmartExitInfo {
  position_age_days: number;
  r_multiple: number;
  exit_score: number;
  quality: string;
  factors: SmartExitFactorScores;
  signals: SmartExitSignals;
  recommendation: string;
  final: string;
  reasoning: string[];
  trigger: string;
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
  /** SL/TP move tracking (migration 021) — badge + tooltip on monitor. */
  initial_stop_loss: number | null;
  initial_take_profit: number | null;
  sl_moved_at: string | null;
  sl_move_reason: string;
  tp_moved_at: string | null;
  tp_move_reason: string;
  exit_info?: SmartExitInfo | null;
  // Explainability: R ปัจจุบัน, $ เสี่ยงถ้าโดน SL, ที่มาราคา
  // (spot/daily/broker/entry), ขั้นตอนคำนวณทีละขั้น — ตารางมอนิเตอร์แสดง
  r_multiple?: number | null;
  risk_amount?: number | null;
  price_source?: string | null;
  calc_notes?: string[] | null;
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
  /** Live portfolio value from DB — capital + realized + unrealized PnL */
  equity: number;
  pnl: number;
  /** Real Risk Engine status computed server-side (same inputs as the worker). */
  risk?: RiskStatus | null;
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

/** Response of POST /api/trading/stats/reset (🗑 รีเซ็ตสถิติ on monitor). */
export interface StatsResetResult {
  ok: boolean;
  deleted: number;
  message: string;
  /** Fresh stats after the reset (same shape as MonitorStats). */
  stats: MonitorStats;
  warnings: string[];
}

/** One point of the equity curve (GET /api/trading/equity-curve). */
export interface EquityPoint {
  date: string;
  equity: number;
}

/** Response of GET /api/trading/equity-curve (performance page chart). */
export interface EquityCurve {
  points: EquityPoint[];
  latest_equity: number;
  peak_equity: number;
  drawdown_pct: number;
  capital: number;
  /** true when no snapshots exist yet (flat line at settings capital) */
  synthetic: boolean;
}

/** Per-ticket result inside POST /api/trading/positions/close-all. */
export interface CloseAllItem {
  ticket: string;
  asset: string;
  ok: boolean;
  pnl?: number;
  exit_price?: number;
  message?: string;
}

/** Response of POST /api/trading/positions/close-all (ปิดทั้งหมด button). */
export interface CloseAllResult {
  ok: boolean;
  closed: number;
  failed: number;
  total_pnl: number;
  results: CloseAllItem[];
  message: string;
}

/** One aggregated row of GET /api/trading/signal-report. */
export interface SignalReportRow {
  key: string;
  trades: number;
  win_rate_pct: number;
  total_pnl: number;
}

/** Response of GET /api/trading/signal-report (signal quality feedback). */
export interface SignalReport {
  days: number;
  signals: number;
  matched_trades: number;
  by_asset: SignalReportRow[];
  by_confidence_band: SignalReportRow[];
  by_regime: SignalReportRow[];
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

// ---------- LINE: notification targets + test button ----------
/** One registered chat from GET /api/line/targets (group/room auto-registered
 * by the webhook, or a personal line_users row). */
export interface LineTarget {
  target_id: string;      // groupId / roomId / userId (C... / U...)
  target_type: string;    // group | room | user
  notification_enabled: boolean;
  created_at?: string | null;
  last_seen_at?: string | null;  // latest webhook event received from this chat
}

/** Response of GET /api/line/targets. */
export interface LineTargetsResponse {
  targets: LineTarget[];
  users: LineTarget[];
}

/** Per-target result of POST /api/line/test. */
export interface LineTestItem {
  target_id: string;
  target_type: string;
  ok: boolean;
  error?: string;   // raw LINE API error when ok=false
}

/** Response of POST /api/line/test (🔔 ทดสอบ button). */
export interface LineTestResult {
  ok: boolean;
  sent: number;
  failed: number;
  results: LineTestItem[];
  hint: string;
}

/** Response of POST /api/line/targets (manual groupId add). */
export interface LineTargetMutation {
  ok: boolean;
  message: string;
  target?: LineTarget;
}

/** Response of GET /api/line/diag (one-shot setup diagnosis). */
export interface LineDiag {
  token_set: boolean;
  secret_set: boolean;
  bot_user_id_set: boolean;
  bot_user_id: string;
  db_available: boolean;
  targets_table_ok: boolean;
  targets_count: number;
  users_count: number;
  table_error?: string;
  token_valid?: boolean;
  bot_user_id_from_api?: string;
  display_name?: string;
  hint?: string;
}

/** One webhook activity record from GET /api/line/events. */
export interface LineEvent {
  at: string;
  kind: string;   // received | signature_rejected | skipped_* | replied | simulated
  [key: string]: unknown;
}

/** Response of GET /api/line/events (webhook debug log). */
export interface LineEventsResponse {
  events: LineEvent[];
}

/** One pipeline step of POST /api/line/simulate. */
export interface SimStep {
  step: string;
  ok: boolean;
  note?: string;
  via?: string;
  reply?: string;
}

/** Response of POST /api/line/simulate (debug: what would the bot reply?). */
export interface LineSimulateResult {
  ok: boolean;
  reply: string | null;
  via?: string;
  steps: SimStep[];
  note?: string;
  pushed?: boolean;
  push_error?: string;
}

export interface PinLoginResponse {
  ok: boolean;
  token: string | null;
  message: string;
  remaining_attempts: number | null;
  locked_until: string | null;
}

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
