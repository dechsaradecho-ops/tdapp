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
| Notifications | LINE Messaging API **+** Web Push (VAPID) — both transports run side by side |
| Deployment | Render.com — `tdapp-api` (web, workers embedded) + `tdapp-web` (static) |

## Project Structure

```
tdapp/
├── frontend/                 # Next.js static-export dashboard → tdappstatic.onrender.com
│   └── src/
│       ├── app/              # Pages: dashboard (หน้าหลัก, รวม market + การ์ดสถานะ
│       │                     #         auto-trade readiness), signals (แท็บ
│       │                     #         signal-logs), monitor (แท็บ performance, รวม risk
│       │                     #         เป็นการ์ดล่างสุด), logs (แท็บ quotes/news/
│       │                     #         scheduler/guard/gate/audit), settings, chat +
│       │                     #         redirect stubs (market/risk/signal-logs/performance)
│       ├── components/       # ChatWidget, CapitalSync, GoalForm, PinManager, MobileNav, ...
│       └── lib/              # api.ts (REST client), portfolio.ts (DB-backed store),
│                             # chat_history.ts, auth.ts (PIN token), types.ts
├── backend/
│   ├── app/
│   │   ├── api/              # REST endpoints (FastAPI routers: auth, trading, settings, chat, ...)
│   │   ├── core/             # Settings, logging, AI config
│   │   ├── engine/           # GoalEngine, RiskEngine, StrategyEngine, PortfolioEngine
│   │   ├── integrations/     # Broker adapters, AI providers, quotes (Twelve Data), LINE client,
│   │   │                     #   web_push (VAPID sender)
│   │   ├── models/           # Pydantic schemas
│   │   ├── services/         # DB access, execution (paper trades), PIN auth, quote log
│   │   └── workers/          # Market Scanner, News Analysis, Auto Trader, Notifier, ...
│   ├── scripts/              # Ops probes: check_*.py, poll_*.py, smoke_stream.py, gen_vapid_keys.py
│   └── tests/                # Pytest suite (882 tests)
├── database/                 # Supabase migrations 001–042 (run manually in SQL Editor)
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

Run the test suite (882 tests):

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
| Notification Service | event/scheduled | LINE **+** Web Push alerts, daily/weekly/monthly reports (critical = immediate) |

Run workers:

```bash
cd backend
python -m app.workers.run_all
```

On Render the workers also run inside the `tdapp-api` web service via
`ENABLE_WORKERS=1` (see `render.yaml`) — do not enable both places at once.

### Ticket numbers are never recycled

`PaperBroker` keeps its order sequence (`_seq`) in memory, so every restart started it at 0 and
re-issued tickets that permanent tables still remembered. Prod 2026-09-14 hit exactly that: the
open **AUDCHF** position was `PAPER-000001`, the ticket the **AUDNZD** trade had been closed with
an hour earlier (after `POST /api/trading/stats/reset` deleted the closed rows) — so the monitor's
“ประวัติ SL/TP” popup, which groups `signal_logs` by ticket, showed the old AUDNZD close under the
new symbol. Fixes:

1. at boot `position_guard.seed_order_sequence` walks `_seq` past the highest ticket the DB still
   holds in **both** `paper_trades` (journal) and `signal_logs` (the timeline source, 7-day TTL) —
   a number is reused only once nothing remembers it, i.e. when there is no history left to mix up;
2. `execution.close_trade_rows(..., asset=, direction=)` refuses to close a row whose symbol/ฝั่ง
   does not match the book (logs a warning instead) — a recycled ticket can never close another
   trade's journal row;
3. the monitor only accepts a log into a position's timeline when the asset matches **and** the log
   is not older than the row itself, so recycled history stays out of the popup.

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

## Push Notifications (Web Push / VAPID)

A second delivery channel **next to LINE** (both fire — neither replaces the other) that reaches
**the OS notification tray** on the phone/desktop even when the tab is closed.

- Subscribe from **Settings → "การแจ้งเตือนมือถือ"** → `เปิดการแจ้งเตือนบนอุปกรณ์นี้`.
  The **ทดสอบการแจ้งเตือน** button does a real round trip and prints per-device results.
- Android / desktop: works from an ordinary browser tab, **no install required**
  (Chrome, Firefox, Samsung Internet). iOS/iPadOS **16.4+**: add the site to the Home Screen
  first (Safari only) — otherwise the card says so instead of pretending it worked.
- Secrets stay server-side: `GET /api/push/key` returns the public key only, and
  `GET /api/push/subscriptions` never returns `endpoint` / `p256dh` / `auth`.
- One row per device in `push_subscriptions` (**migration 042**); health = `fail_count` +
  `last_error`. `410 Gone` disables that row at once, other errors at `MAX_FAILS = 10`.
  Re-subscribing the same endpoint re-enables it and resets the counter — the row is kept,
  never deleted, so the history of a dead device stays visible.
- Delivery: critical types (`risk_warning`, `stop_loss`, `economic_news`, `trade_opened`,
  `trade_closed`, `limit_expand`) push immediately; everything else rides the notification
  queue and pushes when the worker dispatches it (see `notification_worker`).
- Service worker `frontend/public/sw.js` handles `push` / `notificationclick` /
  `pushsubscriptionchange`; the page re-registers a rotated subscription on load
  (`lib/push.ts` → `installPushSync`) because a service worker cannot read the PIN token.
- **Env (required to enable):** `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`
  (`mailto:` or the site URL) — generate with `python backend/scripts/gen_vapid_keys.py`.
  Without them nothing crashes: `/api/push/key` answers `enabled: false` and the card explains it.
- ⚠️ Rotating `VAPID_PRIVATE_KEY` invalidates **every** existing subscription — all devices must
  subscribe again.

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

Database migrations (`database/001–042`) are run manually in the Supabase SQL Editor.
Latest: `042_push_subscriptions.sql` — the `push_subscriptions` table for Web Push
(`endpoint` unique, `p256dh` / `auth` keys, `user_agent`, `user_id text`, `enabled`,
`fail_count`, `last_error`, `last_ok_at`). RLS is on with a **service_role-only** policy —
the frontend never reads the table directly, it goes through `/api/push/*`. Nothing
subscribing/pushing works until it is applied, but the API stays up and the Settings card
explains what is missing.
Before it: `041_currency_exposure_preopen.sql` (currency-exposure cap — sums risk per currency
and per direction instead of averaging pairwise correlation — plus the pre-open guards
spread-max %, pre-news flatten window and session filter) and `040_reentry_cooldown.sql`
(re-entry cooldown after a close, so a 1-minute close→reopen loop cannot happen).
Before those: `039_kill_expand_auto_apply.sql` — `trading_settings.kill_expand_auto_apply`
(`boolean not null default true`), the toggle for the risk-limit timeout policy.
**true** (default) = a confirmation window that lapses widens the breached limits
every time; **false** = the system may widen on its own **once per 24 hours** — after
that a lapsing request is closed as `expired` with `decided_by = 'auto:capped'`, no
new prompt is created, and the kill switch closes the book for safety (the guard
stays the fail-safe: it only closes while the limit is still breached).
**Applied on prod 2026-09-14** (`kill_expand_auto_apply = true` confirmed by a raw
PostgREST select of the column). Until the migration is applied the app still works —
the settings PUT skips the unknown column on PostgREST `PGRST204` and the legacy
“widen every time” behaviour stays in place.
Before it: `038_risk_events_user_text.sql` — `risk_events.user_id` `uuid` → `text`
(**applied on prod 2026-09-14**; without it the audit trail stays empty because the app
writes the pseudo-user `demo` and the uuid FK rejects it with `22P02`). Both write paths
now report that error instead of swallowing it, `GET /api/system/db-check` gained a
`risk_audit` step that inserts + deletes a probe row in `risk_events` (rerunnable proof that
the audit path writes), and `GET /api/system/risk-logs` returns `audit_state`
(`ok` / `empty` / `write_failed`) plus the raw error **only** when a write actually failed in
this process. An empty table by itself no longer points at the migration (that wording sent the
owner hunting for a problem that did not exist); when decisions exist without an audit row the
API now states the fact instead of guessing — those rows were written before 038 was applied.
Migrations 038 + 039 were verified live: `kill_expand_auto_apply` reads back
from `trading_settings`, a `risk_events` insert carrying `user_id = 'demo'` returns `201`,
and `GET /api/trading/limit-expand` serves `auto_apply` / `auto_apply_once` / `lapsed_closes`.
Before it: `034_ai_chat_settings.sql` — `ai_model` + `ai_base_url` on `trading_settings`
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
