"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import ai, chat, goal, market, portfolio, risk, signals, webhook
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services.database import Database
from app.integrations.line_client import LineClient
from app.integrations.brokers import PaperBroker


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
    return {"status": "ok", "platform": "AI Wealth & Trading Advisor", "deployment": "render"}
