# AI Wealth & Trading Advisor Platform

Multi-asset trading advisory platform (Forex, Gold/XAUUSD, Crypto, Indices, CFDs) with
AI-driven market analysis, goal feasibility assessment, risk management, and LINE notifications.

> **Disclaimer:** This platform assesses the *probability* of reaching return targets under risk
> constraints. It **never guarantees profit**.

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 14 App Router — static export (`out/`) served as a Render Static Site |
| Backend | FastAPI (Python 3.11+) |
| Database | Supabase PostgreSQL — single source of truth for account data |
| Auth | 6-digit PIN (server-side hashed), in-memory sessions + Bearer token |
| AI Provider | DeepSeek or GLM — model + base URL configurable from the Settings page (falls back to `ai.config.json`) |
| Trading | Paper-trading engine; broker adapters (MT5 / OANDA / IB) behind an adapter interface |
| Notifications | LINE Messaging API |
| Deployment | Render.com — `tdapp-api` (web, workers embedded) + `tdapp-web` (static) |

## Project Structure

```
tdapp/
├── frontend/                 # Next.js static-export dashboard → tdappstatic.onrender.com
│   └── src/
│       ├── app/              # Pages: dashboard (หน้าหลัก, รวม market + การ์ดสถานะ
│       │                     #         auto-trade readiness), signals (แท็บ
│       │                     #         signal-logs), monitor (แท็บ performance, รวม risk
│       │                     #         เป็นการ์ดล่างสุด), logs, settings, chat +
│       │                     #         redirect stubs (market/risk/signal-logs/performance)
│       ├── components/       # ChatWidget, CapitalSync, GoalForm, PinManager, MobileNav, ...
│       └── lib/              # api.ts (REST client), portfolio.ts (DB-backed store),
│                             # chat_history.ts, auth.ts (PIN token), types.ts
├── backend/
│   ├── app/
│   │   ├── api/              # REST endpoints (FastAPI routers: auth, trading, settings, chat, ...)
│   │   ├── core/             # Settings, logging, AI config
│   │   ├── engine/           # GoalEngine, RiskEngine, StrategyEngine, PortfolioEngine
│   │   ├── integrations/     # Broker adapters, AI providers, quotes (Twelve Data), LINE client
│   │   ├── models/           # Pydantic schemas
│   │   ├── services/         # DB access, execution (paper trades), PIN auth, quote log
│   │   └── workers/          # Market Scanner, News Analysis, Auto Trader, Notifier, ...
│   ├── scripts/              # Ops probes: check_*.py, poll_*.py, smoke_stream.py, ...
│   └── tests/                # Pytest suite (676 tests)
├── database/                 # Supabase migrations 001–034 (run manually in SQL Editor)
├── UI-DESIGN-SYSTEM.md       # iOS Liquid Glass Dark — hard rules for UI work
├── docker-compose.yml        # Local infra (redis)
└── render.yaml               # Render.com blueprint (api + workers + static web)
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

Run the test suite (415 tests):

```bash
cd backend
python -m pytest tests -q
```

### Frontend

```bash
cd frontend
npm install
# point the app at the backend (API CORS currently allows prod origins only)
$env:NEXT_PUBLIC_API_BASE_URL = "https://tdapp-api.onrender.com"   # PowerShell
npm run dev
```

Production build is a static export into `out/` (same env var, then `npx next build`).

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

On Render the workers also run inside the `tdapp-api` web service via
`ENABLE_WORKERS=1` (see `render.yaml`) — do not enable both places at once.

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

## PIN Authentication

Single-user dashboard — no Supabase Auth:

1. Enter the 6-digit PIN → `POST /api/auth/login` returns a Bearer session token.
2. Token is stored in `localStorage` (`tdapp_session_token`) and attached to every API call.
3. Sessions live **in memory** on the backend → every backend deploy wipes them and the UI
   returns to the PIN screen. Seeing many 401s at once right after a deploy = re-login, not a bug.
4. PINs are added/removed in Settings (hashed server-side, `database/008_pin_auth.sql`).

## Data Ownership — Supabase vs localStorage

| Data | Stored in |
|---|---|
| Capital, PnL, equity snapshots, paper trades, signals | Supabase (source of truth) |
| App settings + risk limits | `trading_settings` row (Supabase) |
| Monitor / signals refresh intervals | `trading_settings` (migration 019 — moved out of localStorage) |
| AI model name + base URL | `trading_settings` (migration 034 — `ai_model`, `ai_base_url`) |
| Wallpaper + hero image, hero dim, session token, AI chat history | `localStorage` — device-local UI state only |

- Portfolio numbers come from `/api/trading/monitor` (unrealized PnL computed from open positions).
- รีเซ็ตสถิติ (stats reset) deletes closed trades and wipes + reseeds `equity_snapshots` to capital.
- The Risk card (on the Monitor page) waits until the portfolio store has loaded
  before calling `/api/risk/check` (the endpoint requires capital > 0 — calling
  early returns 422).

## AI Chat

- `/chat` page + floating widget — both stream from `POST /api/chat/stream`.
- Keeps the **last 20 messages** (user + assistant) in `localStorage` (`tdapp_chat_history`)
  across page loads, and sends the same window as model context.
- While waiting: bouncing-dots "AI กำลังคิด..." indicator with an elapsed-seconds counter,
  visible for the entire stream.

### Model & base URL (Settings → "AI Chat — โมเดล & URL")

- **Precedence:** Settings page (DB) → `backend/ai.config.json` → provider default. Leaving a
  field blank keeps whatever the file says. Applies to the chat, LINE commands and the
  explain/journal calls — **immediately, no redeploy** (`get_ai_provider()` is built per request).
- The **API key is never stored in the DB** — it stays in the `AI_API_KEY` env var of the service.
- `POST /api/ai/test` powers the "ทดสอบการเชื่อมต่อ" button: it uses the values **typed in the
  form (still unsaved)**, does a one-line round trip and restores the saved config afterwards,
  so a new gateway/model can be verified before saving. Exposed as `GET /health` → `ai_config`
  (`model_source` / `url_source` = `settings` | `config-file`).
- Base URL accepts a pasted full endpoint — a trailing `/chat/completions` is trimmed.

## Deployment (Render.com)

`render.yaml` blueprint — 2 services:

- `tdapp-api` (web) → FastAPI `uvicorn app.main:app` + workers embedded (`ENABLE_WORKERS=1`)
  → https://tdapp-api.onrender.com
- `tdapp-web` (static) → Next.js static export `out/` → https://tdappstatic.onrender.com
  (SPA rewrite fallback to `index.html`)
- Redis is commented out in the blueprint (unused in code — enable when a real cache/queue lands)
- แยก worker service (tdapp-workers) ถูกตัดออกแล้ว — ถ้ารันคู่กับ ENABLE_WORKERS=1 จะยิง
  order/แจ้งเตือนซ้ำสองเท่า (ดู comment ใน render.yaml หากต้องการเปิดกลับ)

Database migrations (`database/001–034`) are run manually in the Supabase SQL Editor.
Latest: `034_ai_chat_settings.sql` — `ai_model` + `ai_base_url` on `trading_settings`
(AI chat model/base URL editable from the Settings page). Until it is applied, saving still
works — the settings PUT skips unknown columns on PostgREST `PGRST204` and says so in the reply.

## Development Rules (AI safety contract)

✅ Market / News / Sentiment analysis, opportunity assessment, profit feasibility, trade explanation,
portfolio recommendation, risk assessment, chat assistant, daily summary.

❌ Never guarantee profit, never fabricate returns, never exceed user risk limits, never skip risk
management, never open orders beyond max drawdown.

UI work must follow `UI-DESIGN-SYSTEM.md` (iOS Liquid Glass Dark) — e.g. green/red pills inside
`td` must be `<span>` wrappers, and selects must stay translucent (never put `bg-surface` on top
of the glass form styles).
