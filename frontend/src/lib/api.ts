import {
  API_BASE,
  DbCheckResult,
  DbCounts,
  GoalAssessment,
  MarketSummary,
  PortfolioRecommendation,
  RiskStatus,
  SignalProposal,
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
};
