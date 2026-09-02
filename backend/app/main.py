"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import ai, chat, goal, market, portfolio, risk, signals, webhook
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services.database import Database
from app.services.notification_service import NotificationService
from app.integrations.line_client import LineClient
from app.integrations.brokers import PaperBroker
from app.workers import market_scanner, news_analysis, notification_worker, portfolio_monitor

log = logging.getLogger(__name__)


async def _safe_job(coro) -> None:
    """Run a scheduled coroutine, logging failures instead of crashing the app."""
    try:
        await coro
    except Exception:
        log.exception("Worker job failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.db = Database()
    app.state.line = LineClient()
    app.state.broker = PaperBroker()
    await app.state.broker.connect()

    # Background workers run inside this single web service when
    # ENABLE_WORKERS=1 (set on tdapp-api only — never on more than one
    # instance, or jobs will run duplicated).
    scheduler: AsyncIOScheduler | None = None
    if get_settings().enable_workers:
        scheduler = AsyncIOScheduler()
        db = app.state.db
        notifier = NotificationService(db, app.state.line)
        scheduler.add_job(lambda: asyncio.create_task(_safe_job(market_scanner.scan_once(db))),
                          "interval", minutes=5, id="market_scanner", max_instances=1)
        scheduler.add_job(lambda: asyncio.create_task(_safe_job(news_analysis.analyze_once(db))),
                          "interval", minutes=15, id="news_analysis", max_instances=1)
        scheduler.add_job(lambda: asyncio.create_task(_safe_job(asyncio.to_thread(
            portfolio_monitor.monitor_once, db, app.state.broker, notifier))),
            "interval", minutes=1, id="portfolio_monitor", max_instances=1)
        scheduler.add_job(lambda: asyncio.create_task(_safe_job(
            notification_worker.dispatch_pending(db, notifier))),
            "interval", minutes=1, id="notifications", max_instances=1)
        scheduler.start()
        log.info("In-app workers ENABLED: scanner(5m) news(15m) monitor(1m) notify(1m)")
    else:
        log.info("In-app workers disabled (ENABLE_WORKERS not set)")

    yield

    if scheduler is not None:
        scheduler.shutdown(wait=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.db = Database()
    app.state.line = LineClient()
    app.state.broker = PaperBroker()
    await app.state.broker.connect()
    yield


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
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


@app.get("/health", tags=["system"])
async def health() -> dict:
    return {
        "status": "ok",
        "platform": "AI Wealth & Trading Advisor",
        "deployment": "render",
        "workers": "enabled" if get_settings().enable_workers else "disabled",
    }
