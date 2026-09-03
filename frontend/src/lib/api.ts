import {
  API_BASE,
  AppSettings,
  BacktestConfig,
  BacktestResult,
  CorrelationResponse,
  DbCheckResult,
  DbCounts,
  ExtendedAnalysis,
  FrequencyDecision,
  GoalAssessment,
  JournalAnalysis,
  KillSwitch,
  MarketSummary,
  NewsRisk,
  OrderPlan,
  PaperTrading,
  PortfolioRecommendation,
  RiskStatus,
  SessionStatus,
  SettingsSaveResult,
  SignalProposal,
  WalkForwardResult,
} from "./types";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`POST ${path} → ${res.status}`);
  return res.json();
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`PUT ${path} → ${res.status}`);
  return res.json();
}

export const api = {
  // POST /api/goal/assess
  assessGoal: (input: {
    capital: number;
    target_return_pct: number;
    risk_profile: string;
    max_drawdown_pct: number;
    trading_mode: string;
  }) => post<GoalAssessment>("/api/goal/assess", input),

  // GET /api/market/summary
  marketSummary: () => get<MarketSummary>("/api/market/summary"),

  // POST /api/portfolio/recommend
  recommendPortfolio: (input: {
    capital: number;
    target_return_pct: number;
    max_drawdown_pct: number;
    risk_profile: string;
  }) => post<PortfolioRecommendation>("/api/portfolio/recommend", input),

  // POST /api/risk/check
  checkRisk: (input: {
    starting_capital: number;
    peak_equity: number;
    current_equity: number;
    realized_pnl_today: number;
    realized_pnl_week: number;
    realized_pnl_month: number;
    open_risk: number;
  }) => post<RiskStatus>("/api/risk/check", input),

  // GET /api/signals/latest
  latestSignals: () => get<SignalProposal[]>("/api/signals/latest"),

  // GET /api/system/db-check — live insert/select/delete probe
  dbCheck: () => get<DbCheckResult>("/api/system/db-check"),

  // GET /api/system/counts — worker table row counts
  dbCounts: () => get<DbCounts>("/api/system/counts"),

  // ---------- Extended Trading System ----------
  tradingFrequency: () => get<FrequencyDecision>("/api/trading/frequency"),

  tradingOrderPlan: (input: {
    asset: string;
    direction: string;
    entry: number;
    stop_loss: number;
    take_profit: number;
    atr_pct?: number;
    regime?: string;
    equity?: number;
    risk_per_trade_pct?: number;
  }) => post<OrderPlan>("/api/trading/order-plan", input),

  tradingCorrelation: () => get<CorrelationResponse>("/api/trading/correlation"),

  tradingCalendar: () => get<NewsRisk>("/api/trading/calendar"),

  tradingSession: () => get<SessionStatus>("/api/trading/session"),

  tradingKillSwitch: () => get<KillSwitch>("/api/trading/kill-switch"),

  tradingJournal: (days = 30) =>
    get<JournalAnalysis>(`/api/trading/journal?days=${days}`),

  tradingBacktest: (config: BacktestConfig) =>
    post<BacktestResult>("/api/trading/backtest", config),

  tradingWalkForward: (config: BacktestConfig) =>
    post<WalkForwardResult>("/api/trading/walk-forward", config),

  tradingPaper: () => get<PaperTrading>("/api/trading/paper-trading"),

  extendedAnalysis: () => get<ExtendedAnalysis>("/api/trading/extended-analysis"),

  // ---------- Settings ----------
  getSettings: () => get<AppSettings>("/api/settings"),

  saveSettings: (data: Partial<AppSettings>) =>
    put<SettingsSaveResult>("/api/settings", data),

  resetSettings: () => post<SettingsSaveResult>("/api/settings/reset", {}),
};
