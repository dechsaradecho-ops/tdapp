"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (ai, auth, chat, goal, market, portfolio, risk, settings as settings_routes, signals,
                            system, trading, webhook)
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.integrations.ai_provider import get_ai_provider
from app.services.database import Database
from app.services.notification_service import NotificationService
from app.services import quote_log
from app.integrations.line_client import LineClient
from app.integrations.brokers import PaperBroker
from app.workers import (auto_trader, calendar_sync, daily_digest,
                         market_scanner, news_analysis, notification_worker,
                         portfolio_monitor, position_guard)

log = logging.getLogger(__name__)


async def _safe_job(coro) -> None:
    """Run a scheduled coroutine, logging failures instead of crashing the app."""
    try:
        await coro
    except Exception:
        log.exception("Worker job failed")


def _safe(factory):
    """Wrap a coroutine factory in a coroutine function APScheduler can await.

    GOTCHA (prod 2026-09-03): scheduling a plain sync lambda that calls
    asyncio.create_task() silently never runs — AsyncIOScheduler executes sync
    callables in an executor thread with NO running event loop, so every tick
    raised "RuntimeError: no running event loop" before the task was created
    (all six workers quietly did nothing; scanner wrote nothing, position
    guard never marked/closed). A coroutine function is awaited directly on
    the scheduler's event loop and works.
    """
    async def _job() -> None:
        await _safe_job(factory())
    return _job


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.db = Database()
    app.state.line = LineClient()
    app.state.broker = PaperBroker()
    await app.state.broker.connect()
    # Quote API call log (7-day auto-expiry) writes through this module-level
    # reference — quotes.py logs every external price fetch via quote_log.
    quote_log.set_db(app.state.db)

    # The PaperBroker book is in-memory — restore any DB rows still marked
    # "open" so position_guard can enforce their SL/TP again after a restart
    # (otherwise positions survive restarts in the DB but lose enforcement).
    try:
        await position_guard.rehydrate_book(app.state.db, app.state.broker)
    except Exception:
        log.exception("broker book rehydrate failed (continuing)")

    # Background workers run inside this single web service when
    # ENABLE_WORKERS=1 (set on tdapp-api only — never on more than one
    # instance, or jobs will run duplicated).
    scheduler: AsyncIOScheduler | None = None
    if get_settings().enable_workers:
        scheduler = AsyncIOScheduler()
        db = app.state.db
        notifier = NotificationService(db, app.state.line)
        scheduler.add_job(_safe(lambda: market_scanner.scan_once(db)),
                          "interval", minutes=5, id="market_scanner", max_instances=1)
        scheduler.add_job(_safe(lambda: news_analysis.analyze_once(db)),
                          "interval", minutes=15, id="news_analysis", max_instances=1)
        scheduler.add_job(_safe(lambda: asyncio.to_thread(
            portfolio_monitor.monitor_once, db, app.state.broker, notifier)),
            "interval", minutes=1, id="portfolio_monitor", max_instances=1)
        scheduler.add_job(_safe(lambda:
            notification_worker.dispatch_pending(db, notifier)),
            "interval", minutes=1, id="notifications", max_instances=1)
        scheduler.add_job(_safe(lambda:
            auto_trader.trade_once(db, app.state.broker, notifier)),
            "interval", minutes=1, id="auto_trader", max_instances=1)
        scheduler.add_job(_safe(lambda:
            position_guard.guard_once(db, app.state.broker, notifier)),
            "interval", minutes=1, id="position_guard", max_instances=1)
        scheduler.add_job(_safe(lambda: calendar_sync.sync_once(db)),
                          "interval", hours=6, id="calendar_sync", max_instances=1)
        scheduler.add_job(_safe(lambda:
            daily_digest.send_digest_once(db, notifier)),
            "interval", minutes=60, id="daily_digest", max_instances=1)
        scheduler.start()
        app.state.scheduler = scheduler
        log.info("In-app workers ENABLED: scanner(5m) news(15m) monitor(1m) "
                 "notify(1m) auto_trader(1m) position_guard(1m) "
                 "calendar_sync(6h) daily_digest(1h, idempotent)")
    else:
        app.state.scheduler = None
        log.info("In-app workers disabled (ENABLE_WORKERS not set)")

    yield

    if app.state.scheduler is not None:
        app.state.scheduler.shutdown(wait=False)


settings = get_settings()

app = FastAPI(
    title="AI Wealth & Trading Advisor API",
    version="0.1.0",
    description=(
        "Multi-asset trading advisory API. Provides goal feasibility assessment, "
        "market regime analysis, opportunity scoring, portfolio recommendation and "
        "risk management. Probabilistic only — never guarantees profit."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# PIN gate — every /api/* request needs a valid session token, except the
# whitelist below. The token comes from POST /api/auth/login (6-digit PIN).
# Fail-CLOSED: if the check errors out, the request is rejected.
#
# Registered BEFORE CORSMiddleware (last-added = outermost) so CORS wraps the
# gate's 401s with Access-Control-Allow-Origin. Without this the browser
# blocks the 401 entirely and the frontend can never react to it (the PIN pad
# and re-lock flow silently break).
# ---------------------------------------------------------------------------
_PIN_EXEMPT_PATHS = {
    "/ping", "/health",
    "/api/auth/status", "/api/auth/login", "/api/auth/set-pin",
    # LINE servers call the webhook — they have no PIN session. The webhook
    # verifies its own HMAC signature (X-Line-Signature) instead.
    "/api/line/webhook",
}


@app.middleware("http")
async def pin_gate(request, call_next):
    from app.services import pin_auth
    path = request.url.path
    if request.method == "OPTIONS":
        return await call_next(request)   # CORS preflight has no auth header
    if path in _PIN_EXEMPT_PATHS or not path.startswith("/api/"):
        return await call_next(request)
    # Gate is enforced only once a PIN exists (bootstrap stays open until
    # the owner sets their PIN from the dashboard).
    if not pin_auth.gate_active(request.app.state.db):
        return await call_next(request)
    header = request.headers.get("authorization") or ""
    token = header[7:] if header.startswith("Bearer ") else ""
    if not pin_auth.session_valid(token):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(goal.router, prefix="/api/goal", tags=["goal"])
app.include_router(market.router, prefix="/api/market", tags=["market"])
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])
app.include_router(risk.router, prefix="/api/risk", tags=["risk"])
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(ai.router, prefix="/api/ai", tags=["ai"])
app.include_router(webhook.router, prefix="/api/line", tags=["line"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(trading.router, prefix="/api/trading", tags=["trading"])
app.include_router(settings_routes.router, prefix="/api/settings", tags=["settings"])
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])


@app.get("/ping", tags=["system"])
async def ping() -> dict:
    """Ultra-light keepalive for external cron/uptime pingers.

    Deliberately touches nothing (no DB client, no AI provider, no scheduler
    inspection) so it stays fast even on a cold or degraded instance.
    Point a cron-job at this every ~10 min to prevent Render spin-down.
    """
    return {"pong": True}


@app.get("/health", tags=["system"])
async def health() -> dict:
    """Liveness + integration diagnostics (used by scripts/check_live.py)."""
    db: Database = app.state.db
    provider = get_ai_provider()
    scheduler = getattr(app.state, "scheduler", None)
    return {
        "status": "ok",
        "platform": "AI Wealth & Trading Advisor",
        "deployment": "render",
        "workers": ("running" if scheduler is not None
                    else ("enabled" if get_settings().enable_workers else "disabled")),
        "jobs": [j.id for j in scheduler.get_jobs()] if scheduler is not None else [],
        "db": "ok" if db.available else "unavailable",
        "db_detail": (db.init_error or "connected"),
        "ai": provider.name,
    }
