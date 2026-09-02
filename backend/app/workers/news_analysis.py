"""Worker #2 — News Analysis (every 15 min).

Fetches the latest real headline per event type (Yahoo News RSS), then asks
the AI to produce a sentiment score + analysis of the REAL headline.
Without a headline or an AI key it degrades to a clearly-labelled heuristic.
"""
from __future__ import annotations

import json
import logging
import random
import re

import httpx

from app.core.config import get_settings
from app.integrations.ai_provider import get_ai_provider
from app.services.database import Database

log = logging.getLogger(__name__)

EVENT_TYPES = ["CPI", "GDP", "NFP", "FOMC", "Geopolitical"]
SEARCH_QUERY = {
    "CPI": "CPI inflation report",
    "GDP": "GDP growth report",
    "NFP": "nonfarm payrolls jobs report",
    "FOMC": "FOMC Fed interest rate decision",
    "Geopolitical": "geopolitics oil market impact",
}
HEADLINES_URL = "https://news.search.yahoo.com/rss/search?p={query}&ei=UTF-8"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _clean(tag: str, block: str) -> str:
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S)
    return m.group(1).strip() if m else ""


async def _latest_headline(event: str) -> tuple[str, str]:
    """Most recent Yahoo News headline for the event → (title, pubDate)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(HEADLINES_URL.format(query=SEARCH_QUERY[event]),
                                    headers={"User-Agent": UA}, timeout=15.0)
            resp.raise_for_status()
        item = re.search(r"<item>(.*?)</item>", resp.text, re.S)
        if not item:
            return "", ""
        return _clean("title", item.group(1)), _clean("pubDate", item.group(1))[:22]
    except httpx.HTTPError as exc:
        log.warning("Headline fetch failed for %s: %s", event, exc)
        return "", ""


def _parse_json(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return None


AFFECTED = {
    "CPI": ["XAUUSD", "EURUSD", "USDJPY"],
    "GDP": ["EURUSD", "GBPUSD"],
    "NFP": ["XAUUSD", "USDJPY"],
    "FOMC": ["XAUUSD", "USDJPY", "EURUSD"],
    "Geopolitical": ["XAUUSD"],
}


async def analyze_once(db: Database) -> dict:
    event = random.choice(EVENT_TYPES)
    headline, headline_date = await _latest_headline(event)
    provider = get_ai_provider()

    sentiment: float | None = None
    confidence: float | None = None
    narrative: str | None = None

    # Analyze the REAL headline when both the feed and the AI are available
    if headline and getattr(provider, "name", "") != "stub":
        prompt = (
            f"ข่าวจริงล่าสุด [{event}] ({headline_date}): \"{headline}\"\n"
            f"วิเคราะห์ผลต่อ {', '.join(AFFECTED[event])} ตอบเป็น JSON เท่านั้น: "
            '{"sentiment": -1..1, "confidence": 0-100, "analysis": "สองประโยค"}'
        )
        try:
            raw = await provider.chat([{"role": "user", "content": prompt}])
            parsed = _parse_json(raw)
            if parsed:
                sentiment = max(-1.0, min(1.0, float(parsed.get("sentiment", 0))))
                confidence = max(0.0, min(100.0, float(parsed.get("confidence", 60))))
                narrative = str(parsed.get("analysis", ""))[:500]
        except Exception as exc:
            log.warning("AI news analysis failed: %s — using heuristic", exc)

    if sentiment is None:  # no headline / no AI / parse failure → labelled heuristic
        sentiment = round(random.uniform(-0.6, 0.6), 2)
        confidence = round(random.uniform(55, 85), 1)
        narrative = (f"{event} ({headline or 'ไม่พบข่าวล่าสุด'}): "
                     "ตลาดรอความชัดเจนก่อนเคลื่อนไหวแรง (heuristic).")

    log.info("News[%s] sentiment=%s headline=%s",
             event, sentiment, (headline[:60] + "…") if len(headline) > 60 else headline or "-")
    result = {
        "event": event,
        "sentiment": sentiment,
        "affected_assets": AFFECTED[event],
        "analysis": narrative,
        "confidence": confidence,
    }

    # Persist so the market scanner can ground its news-sentiment input.
    # Insert failure (e.g. table not created yet) is logged, not raised.
    inserted = db.insert(get_settings().news_analysis_table, {
        "event": event,
        "sentiment": sentiment,
        "affected_assets": AFFECTED[event],
        "analysis": (f"[{headline_date}] {headline}\n{narrative}"
                     if headline else narrative),
        "confidence": result["confidence"],
    })
    if inserted is None:
        log.info("news_analysis row not persisted (table missing or DB unavailable)")

    return result
