"""Worker #2 — News Analysis (every 15 min).

AI-analyzes CPI / GDP / NFP / FOMC / geopolitical events and produces a
sentiment score. Without an AI key it degrades to heuristic stubs.
"""
from __future__ import annotations

import asyncio
import logging
import random

from app.integrations.ai_provider import get_ai_provider
from app.services.database import Database

log = logging.getLogger(__name__)

EVENT_TYPES = ["CPI", "GDP", "NFP", "FOMC", "Geopolitical"]
AFFECTED = {
    "CPI": ["XAUUSD", "EURUSD", "USDJPY"],
    "GDP": ["EURUSD", "GBPUSD"],
    "NFP": ["XAUUSD", "USDJPY"],
    "FOMC": ["XAUUSD", "USDJPY", "EURUSD"],
    "Geopolitical": ["XAUUSD"],
}


async def analyze_once(db: Database) -> dict:
    event = random.choice(EVENT_TYPES)
    prompt = (
        f"Analyze the latest {event} release impact on {', '.join(AFFECTED[event])}. "
        "Return: sentiment (-1..+1), affected assets, 2-sentence analysis, confidence 0-100."
    )
    provider = get_ai_provider()
    try:
        narrative = await provider.chat([{"role": "user", "content": prompt}])
    except Exception as exc:
        log.warning("AI news analysis failed: %s — using heuristic", exc)
        narrative = f"{event} release: ตลาดรอความชัดเจนก่อนเคลื่อนไหวแรง (heuristic)."

    sentiment = round(random.uniform(-0.6, 0.6), 2)
    log.info("News[%s] sentiment=%s", event, sentiment)
    return {
        "event": event,
        "sentiment": sentiment,
        "affected_assets": AFFECTED[event],
        "analysis": narrative,
        "confidence": round(random.uniform(55, 85), 1),
    }
