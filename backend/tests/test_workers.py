"""Worker tests with a FakeDatabase — catches the 'silent insert failure' class.

The production bug this suite guards against: Database.insert() swallows all
errors (logs only), so a wrong env/RLS/schema failure makes workers look
healthy while writing NOTHING. These tests use a recording fake and assert
rows would actually be persisted.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_workers.py -v
"""
from __future__ import annotations

import json

import pytest

from app.workers import market_scanner, news_analysis


# ---------------------------------------------------------------------------
# FakeDatabase — records inserts, selectable rows
# ---------------------------------------------------------------------------
class FakeDatabase:
    """In-memory stand-in with the same insert/select/update surface."""

    def __init__(self, rows: dict[str, list[dict]] | None = None,
                 fail_tables: set[str] | None = None):
        self.rows = rows or {}
        self.fail_tables = fail_tables or set()
        self.inserted: list[tuple[str, dict]] = []

    def insert(self, table: str, row: dict) -> dict | None:
        if table in self.fail_tables:
            return None  # mimics Database.insert swallowing the error
        self.inserted.append((table, dict(row)))
        self.rows.setdefault(table, []).insert(0, {**row, "id": f"fake-{len(self.inserted)}"})
        return row

    def select(self, table: str, filters: dict | None = None,
               order: str = "created_at", desc: bool = True, limit: int = 50) -> list[dict]:
        rows = list(self.rows.get(table, []))
        for col, val in (filters or {}).items():
            rows = [r for r in rows if r.get(col) == val]
        return rows[:limit]

    def update(self, table: str, row_id: str, changes: dict) -> bool:
        for r in self.rows.get(table, []):
            if r.get("id") == row_id:
                r.update(changes)
                return True
        return False

    @property
    def available(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Market scanner
# ---------------------------------------------------------------------------
def strong_snapshot(asset: str, news_sentiment: float = 0.0):
    from app.engine.strategy_engine import IndicatorSnapshot
    return IndicatorSnapshot(
        asset=asset, price=100.0, ema_fast=105.0, ema_slow=95.0,
        adx=40.0, supertrend_dir=1, rsi=62.0, macd_hist=2.0,
        atr_pct=0.8, volatility_index=12.0, news_sentiment=news_sentiment,
        high_impact_event=False, source="live")


def choppy_snapshot(asset: str, news_sentiment: float = 0.0):
    from app.engine.strategy_engine import IndicatorSnapshot
    return IndicatorSnapshot(
        asset=asset, price=100.0, ema_fast=100.4, ema_slow=99.6,
        adx=12.0, supertrend_dir=0, rsi=50.0, macd_hist=0.1,
        atr_pct=0.5, volatility_index=8.0, news_sentiment=news_sentiment,
        high_impact_event=False, source="live")


class TestMarketScanner:
    @pytest.mark.asyncio
    async def test_scan_persists_market_analysis_for_all_assets(self, monkeypatch):
        db = FakeDatabase()
        await market_scanner.scan_once(db)
        assets = {row["asset"] for table, row in db.inserted
                  if table == "market_analysis"}
        assert assets == set(market_scanner.SCAN_ASSETS)

    @pytest.mark.asyncio
    async def test_scan_rows_have_required_columns(self, monkeypatch):
        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        await market_scanner.scan_once(db)
        for table, row in db.inserted:
            if table == "market_analysis":
                assert {"asset", "regime", "sentiment", "confidence",
                        "explanation"} <= set(row)
            if table == "signals":
                assert {"asset", "direction", "confidence", "opportunity_score",
                        "entry", "stop_loss", "take_profit", "expected_rr",
                        "approval", "explanation"} <= set(row)
                assert row["approval"] == "pending"

    @pytest.mark.asyncio
    async def test_strong_setup_writes_signal(self, monkeypatch):
        """Force one asset to score >= 70 → a signals row must be inserted."""
        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        await market_scanner.scan_once(db)
        signal_rows = [row for table, row in db.inserted if table == "signals"]
        assert signal_rows, "score>=70 setup must persist a signal row"
        assert all(r["confidence"] >= 70 for r in signal_rows)

    @pytest.mark.asyncio
    async def test_weak_setup_writes_no_signal(self, monkeypatch):
        """Choppy market → no signal rows, but market_analysis still written."""
        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return choppy_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        await market_scanner.scan_once(db)
        assert not [row for table, row in db.inserted if table == "signals"]
        assert [row for table, row in db.inserted if table == "market_analysis"]

    @pytest.mark.asyncio
    async def test_silent_insert_failure_is_detectable(self, monkeypatch):
        """The regression test: when inserts fail (bad env/RLS), the scan still
        'completes' — so callers must verify persistence separately (that is
        exactly what scripts/check_live.py does in production)."""
        db = FakeDatabase(fail_tables={"market_analysis", "signals"})

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        results = await market_scanner.scan_once(db)
        assert len(results) == 5           # scan still completes
        assert db.inserted == []           # but nothing was persisted

    @pytest.mark.asyncio
    async def test_news_sentiment_reaches_snapshot(self, monkeypatch):
        """news_analysis rows in DB must flow into the scanner input."""
        db = FakeDatabase(rows={"news_analysis": [
            {"event": "CPI", "sentiment": 0.8, "affected_assets": ["XAUUSD", "EURUSD"]},
        ]})
        captured: dict[str, float] = {}

        async def snap(asset, news_sentiment=0.0):
            captured[asset] = news_sentiment
            return choppy_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        await market_scanner.scan_once(db)
        assert captured["XAUUSD"] == 0.8
        assert captured["EURUSD"] == 0.8
        assert captured["GBPUSD"] == 0.0  # unaffected asset

    @pytest.mark.asyncio
    async def test_regime_enum_valid_for_db(self, monkeypatch):
        """regime value must be a Postgres enum member, else the insert fails
        silently even though the code 'works'."""
        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        await market_scanner.scan_once(db)
        valid = {"strong_bull_trend", "bull_trend", "sideway", "high_volatility",
                 "bear_trend", "strong_bear_trend", "news_driven_market"}
        regimes = [row["regime"] for table, row in db.inserted
                   if table == "market_analysis"]
        assert regimes, "scanner must produce market_analysis rows"
        assert all(r in valid for r in regimes)


# ---------------------------------------------------------------------------
# News analysis worker
# ---------------------------------------------------------------------------
class SimpleProvider:
    """Minimal AI provider double for worker tests."""

    def __init__(self, name: str, chat_raw: str = ""):
        self.name = name
        self._raw = chat_raw

    async def chat(self, messages) -> str:
        if self.name == "stub":
            raise RuntimeError("stub has no AI")
        return self._raw


async def empty_headline(event: str) -> tuple[str, str]:
    return "", ""


async def real_headline(event: str) -> tuple[str, str]:
    return "Fed signals rate cut as inflation cools", "Tue, 02 Sep 2026"


class TestNewsAnalysis:
    @pytest.mark.asyncio
    async def test_heuristic_fallback_persists_row(self, monkeypatch):
        """No headline + stub AI → labelled heuristic row still persisted."""
        db = FakeDatabase()
        monkeypatch.setattr(news_analysis, "_latest_headline", empty_headline)
        monkeypatch.setattr(news_analysis, "get_ai_provider",
                            lambda: SimpleProvider("stub"))
        await news_analysis.analyze_once(db)
        rows = db.select("news_analysis", limit=10)
        assert len(rows) == 1
        assert rows[0]["event"] in news_analysis.EVENT_TYPES
        assert -1 <= float(rows[0]["sentiment"]) <= 1
        assert 0 <= float(rows[0]["confidence"]) <= 100
        assert "heuristic" in rows[0]["analysis"]

    @pytest.mark.asyncio
    async def test_real_headline_with_ai_is_persisted(self, monkeypatch):
        headline = "Fed signals rate cut as inflation cools"
        db = FakeDatabase()
        monkeypatch.setattr(news_analysis, "_latest_headline", real_headline)
        monkeypatch.setattr(news_analysis, "get_ai_provider",
                            lambda: SimpleProvider("glm", chat_raw=json.dumps({
                                "sentiment": 0.7, "confidence": 82,
                                "analysis": "ดี ต่อทองคำและสกุลยูโร"})))
        await news_analysis.analyze_once(db)
        rows = db.select("news_analysis", limit=10)
        assert len(rows) == 1
        assert headline[:30] in rows[0]["analysis"]
        assert abs(float(rows[0]["sentiment"]) - 0.7) < 1e-6
        assert float(rows[0]["confidence"]) == 82.0
        assert "heuristic" not in rows[0]["analysis"]

    @pytest.mark.asyncio
    async def test_ai_garbage_falls_back_to_heuristic(self, monkeypatch):
        db = FakeDatabase()
        monkeypatch.setattr(news_analysis, "_latest_headline", real_headline)
        monkeypatch.setattr(news_analysis, "get_ai_provider",
                            lambda: SimpleProvider("glm", chat_raw="not json at all"))
        await news_analysis.analyze_once(db)
        rows = db.select("news_analysis", limit=10)
        assert len(rows) == 1
        assert "heuristic" in rows[0]["analysis"]

    @pytest.mark.asyncio
    async def test_affected_assets_match_event(self, monkeypatch):
        db = FakeDatabase()
        monkeypatch.setattr(news_analysis, "_latest_headline", empty_headline)
        monkeypatch.setattr(news_analysis, "get_ai_provider",
                            lambda: SimpleProvider("stub"))
        await news_analysis.analyze_once(db)
        row = db.select("news_analysis", limit=1)[0]
        assert row["affected_assets"] == news_analysis.AFFECTED[row["event"]]

    @pytest.mark.asyncio
    async def test_insert_failure_does_not_crash_worker(self, monkeypatch):
        """Table missing in prod → worker must complete, not raise."""
        db = FakeDatabase(fail_tables={"news_analysis"})
        monkeypatch.setattr(news_analysis, "_latest_headline", empty_headline)
        monkeypatch.setattr(news_analysis, "get_ai_provider",
                            lambda: SimpleProvider("stub"))
        result = await news_analysis.analyze_once(db)  # must not raise
        assert result["event"] in news_analysis.EVENT_TYPES
