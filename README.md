# AI Wealth & Trading Advisor Platform

Multi-asset trading advisory platform (Forex, Gold/XAUUSD, Crypto, Indices, CFDs) with
AI-driven market analysis, goal feasibility assessment, risk management, and LINE notifications.

> **Disclaimer:** This platform assesses the *probability* of reaching return targets under risk
> constraints. It **never guarantees profit**.

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js (App Router), TypeScript, TailwindCSS, TradingView Widget |
| Backend | FastAPI (Python 3.11+) |
| Database | Supabase PostgreSQL |
| Auth | Supabase Auth |
| Realtime | Supabase Realtime |
| Storage | Supabase Storage |
| Cache | Redis |
| AI Provider | DeepSeek API or GLM API (pluggable) |
| Trading | MetaTrader5 / OANDA / Interactive Brokers (adapter pattern) |
| Notifications | LINE Messaging API |
| Deployment | Render.com (Web Service + Worker + Redis) |

## Project Structure

```
tdapp/
├── frontend/                 # Next.js dashboard
│   └── src/
│       ├── app/              # App Router pages (dashboard, market, risk, settings, chat)
│       ├── components/       # UI components (GoalForm, OpportunityScore, TradingViewChart, ...)
│       └── lib/              # API client, types, formatting helpers
├── backend/
│   └── app/
│       ├── api/              # REST endpoints (FastAPI routers)
│       ├── core/             # Settings, logging, security
│       ├── engine/           # GoalEngine, RiskEngine, StrategyEngine, PortfolioEngine
│       ├── integrations/     # Broker adapters (MT5, OANDA, IB), AI providers, LINE client
│       ├── models/           # Pydantic schemas
│       ├── services/         # DB access, notifications
│       └── workers/          # Market Scanner, News Analysis, Portfolio Monitor, Notifier
├── database/                 # Supabase migrations (SQL)
├── docker-compose.yml        # Local infra (redis)
└── render.yaml               # Render.com blueprint
```

## Quick Start (Local)

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows  (use `source .venv/bin/activate` on macOS/Linux)
pip install -r requirements.txt
copy .env.example .env          # then edit values
uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

### Frontend

```bash
cd frontend
npm install
copy .env.example .env.local    # then edit values
npm run dev
```

Dashboard: http://localhost:3000

### Local infra (Redis)

```bash
docker compose up -d redis
```

## Background Workers

| Worker | Interval | Responsibility |
|---|---|---|
| Market Scanner | 5 min | Analyze EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD → trend/volatility/opportunity score |
| News Analysis | 15 min | CPI, GDP, NFP, FOMC, geopolitical events → sentiment score |
| Portfolio Monitor | 1 min | Drawdown, open risk, exposure → auto-pause + close + notify on breach |
| Notification Service | event/scheduled | LINE alerts, daily/weekly/monthly reports (critical = immediate) |

Run workers:

```bash
cd backend
python -m app.workers.run_all
```

## Trading Modes

- **AUTO** — AI analyzes, opens/closes orders, sizes positions (respecting the Risk Engine).
- **SEMI-AUTO** — AI proposes signals; user approves via dashboard or LINE (`[Approve] [Reject] [View Analysis]`).
- **MANUAL** — AI only analyzes and suggests Entry/Exit zones; **never** places orders.

## Risk Engine Defaults

| Parameter | Default |
|---|---|
| Risk per trade | 0.5% |
| Max daily loss | 2% |
| Max weekly loss | 5% |
| Max monthly loss | 8% |
| Max drawdown | 10% |

When any limit is hit → `TRADING PAUSED — MANUAL REVIEW REQUIRED` + LINE Risk Alert.

## LINE Commands

`/portfolio`, `/market`, `/positions`, `/risk`, `/summary`, `/pause`, `/resume`

## Deployment (Render.com)

`render.yaml` blueprint included:
- `web` → FastAPI + Next.js (static export or Node service)
- `worker` → `python -m app.workers.run_all`
- `redis` → Render Redis instance

## Development Rules (AI safety contract)

✅ Market / News / Sentiment analysis, opportunity assessment, profit feasibility, trade explanation,
portfolio recommendation, risk assessment, chat assistant, daily summary.

❌ Never guarantee profit, never fabricate returns, never exceed user risk limits, never skip risk
management, never open orders beyond max drawdown.
