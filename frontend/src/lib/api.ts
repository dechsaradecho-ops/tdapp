import {
  API_BASE,
  AppSettings,
  BacktestConfig,
  BacktestResult,
  ClosePositionResult,
  CorrelationResponse,
  DbCheckResult,
  DbCounts,
  ExtendedAnalysis,
  FrequencyDecision,
  GoalAssessment,
  JournalAnalysis,
  KillSwitch,
  MarketSummary,
  MonitorSnapshot,
  NewsRisk,
  OrderPlan,
  PaperTrading,
  PauseStatus,
  PinLoginResponse,
  PinStatus,
  PortfolioRecommendation,
  QuoteLogsResponse,
  QuoteTestResult,
  RiskStatus,
  SessionStatus,
  SettingsSaveResult,
  SignalLogsResponse,
  StatsResetResult,
  SignalProposal,
  WalkForwardResult,
} from "./types";
import { getToken, notifyAuthExpired } from "./auth";

function headers(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

/** Shared 401 handling: clear the dead token + tell the UI to re-lock.
 *
 * Fires the auth-expired event ONLY when the failed request actually carried
 * a token — background polls made before login (no token) would otherwise
 * spam the event every second and keep resetting the PIN pad's message.
 */
function handle401(res: Response, path: string): never {
  if (res.status === 401) {
    if (getToken()) notifyAuthExpired();
    throw new Error(`ต้องเข้าสู่ระบบ (${path})`);
  }
  throw new Error(`${path} → ${res.status}`);
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store", headers: headers() });
  if (!res.ok) handle401(res, path);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  });
  if (!res.ok) handle401(res, path);
  return res.json();
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify(body),
  });
  if (!res.ok) handle401(res, path);
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

  // ---------- Execution switch (Phase 1) ----------
  getTradingPause: () => get<PauseStatus>("/api/trading/pause"),
  setTradingPause: (paused: boolean, reason = "") =>
    post<PauseStatus>("/api/trading/pause", { paused, reason }),

  // ---------- Monitor dashboard ----------
  monitor: () => get<MonitorSnapshot>("/api/trading/monitor"),

  // POST /api/trading/positions/close — manual close from the monitor page
  closePosition: (ticket: string, close_reason = "manual") =>
    post<ClosePositionResult>("/api/trading/positions/close",
      { ticket, close_reason }),

  // POST /api/trading/stats/reset — 🗑 รีเซ็ตสถิติ (deletes closed trades,
  // keeps open positions; confirm=true required)
  resetStats: () =>
    post<StatsResetResult>("/api/trading/stats/reset", { confirm: true }),

  // ---------- Quote API call log (7-day auto-expiry) ----------
  quoteLogs: (limit = 100) =>
    get<QuoteLogsResponse>(`/api/system/quote-logs?limit=${limit}`),
  quoteTest: () => post<QuoteTestResult>("/api/system/quote-test", {}),

  // ---------- Signal lifecycle log (7-day auto-expiry) ----------
  signalLogs: (limit = 100) =>
    get<SignalLogsResponse>(`/api/system/signal-logs?limit=${limit}`),

  // ---------- Auth: 6-digit PIN gate ----------
  authStatus: () => get<PinStatus>("/api/auth/status"),
  authLogin: (pin: string) =>
    post<PinLoginResponse>("/api/auth/login", { pin }),
  authSetPin: (pin: string) =>
    post<PinLoginResponse>("/api/auth/set-pin", { pin }),
  authLogout: () => post<{ ok: boolean }>("/api/auth/logout", {}),
};
