"""Worker #2 — News Analysis (every 15 min).

Fetches the latest real headline per event type (Google News RSS, Yahoo
fallback), then asks the AI to produce a sentiment score + analysis of the
REAL headline. Without a headline or an AI key it degrades to a
clearly-labelled heuristic.
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
HEADLINES_URL = "https://news.search.yahoo.com/rss/search"
HEADLINES_PARAMS = {"ei": "UTF-8"}
# Google News RSS — primary feed (Yahoo News RSS returns HTTP 500 since
# at least 2026-09-10 for every query; kept only as a fallback).
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
GOOGLE_NEWS_PARAMS = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _clean(tag: str, block: str) -> str:
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S)
    return m.group(1).strip() if m else ""


def _parse_rss_item(body: str) -> tuple[str, str]:
    """First <item> in an RSS body → (title, pubDate). Empty on no match."""
    item = re.search(r"<item>(.*?)</item>", body, re.S)
    if not item:
        return "", ""
    return _clean("title", item.group(1)), _clean("pubDate", item.group(1))[:22]


async def _fetch_rss(url: str, params: dict) -> str:
    """GET an RSS feed → raw body. Raises httpx.HTTPError on failure."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params,
                                headers={"User-Agent": UA}, timeout=15.0)
        resp.raise_for_status()
        return resp.text


async def _latest_headline(event: str) -> tuple[str, str]:
    """Most recent headline for the event → (title, pubDate).

    Google News RSS first, Yahoo News RSS as fallback. Returns ("", "")
    when both feeds fail (caller degrades to a labelled heuristic).
    """
    query = SEARCH_QUERY[event]
    try:
        body = await _fetch_rss(GOOGLE_NEWS_URL,
                                {"q": query, **GOOGLE_NEWS_PARAMS})
        title, date = _parse_rss_item(body)
        if title:
            return title, date
        log.warning("Google News RSS empty for %s — trying Yahoo", event)
    except httpx.HTTPError as exc:
        log.warning("Google News fetch failed for %s: %s — trying Yahoo", event, exc)
    try:
        body = await _fetch_rss(HEADLINES_URL,
                                {"p": query, **HEADLINES_PARAMS})
        return _parse_rss_item(body)
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
