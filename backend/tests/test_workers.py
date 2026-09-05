"""Worker tests with a FakeDatabase — catches the 'silent insert failure' class.

The production bug this suite guards against: Database.insert() swallows all
errors (logs only), so a wrong env/RLS/schema failure makes workers look
healthy while writing NOTHING. These tests use a recording fake and assert
rows would actually be persisted.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_workers.py -v
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

import pytest

from app.workers import (calendar_sync, daily_digest, market_scanner,
                         news_analysis, position_guard, portfolio_monitor)


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
        self._client = object()  # settings loader probes db._client

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

    def insert_raw(self, table: str, row: dict) -> tuple[dict | None, str | None]:
        """Same contract as Database.insert_raw (raw error, no swallow)."""
        if table in self.fail_tables:
            return None, "fake: new row violates row-level security policy"
        self.insert(table, row)
        return row, None

    def delete(self, table: str, filters: dict) -> bool:
        rows = self.rows.get(table, [])
        kept = [r for r in rows
                if not all(r.get(c) == v for c, v in filters.items())]
        self.rows[table] = kept
        return len(kept) < len(rows)

    @property
    def available(self) -> bool:
        return True

    init_error: str | None = None


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
    @pytest.fixture(autouse=True)
    def _no_network_spot(self, monkeypatch):
        """The emit block re-anchors entries at the live spot price — patch
        the feed so tests stay offline and deterministic."""
        async def fake_spot(assets, **_kw):
            return {a: 101.0 for a in assets}, {}
        monkeypatch.setattr(market_scanner.quotes, "fetch_spot_prices", fake_spot)

    @pytest.fixture(autouse=True)
    def _market_open(self, monkeypatch):
        """Tests run on any weekday — force the market OPEN so the weekend
        close guard doesn't suppress the emit block (the closed-market
        behaviour has its own dedicated tests below)."""
        monkeypatch.setattr(market_scanner, "_market_closed",
                            lambda now=None: False)

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
    async def test_frequency_limits_come_from_user_settings(self, monkeypatch):
        """Regression (2026-09-04): the scanner hardcoded FrequencyEngine(moderate)
        (max 6/day) and ignored the user's saved settings — raising max_trades_daily
        to 20 on the Settings page had no effect and strong setups were silently
        throttled. With settings wired, 6 signals today + limit 20 → still emits."""
        from datetime import datetime, timezone

        from app.models.schemas import AppSettings

        db = FakeDatabase()
        today = datetime.now(timezone.utc).date().isoformat()
        for i in range(6):  # 6 signals already emitted today (>= moderate's 6 cap)
            db.insert("signals", {"asset": "EURUSD", "direction": "buy",
                                  "confidence": 75.0,
                                  "created_at": f"{today}T0{i}:00:00Z",
                                  "approval": "approved"})

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        monkeypatch.setattr(market_scanner, "get_app_settings",
                            lambda _db: AppSettings(max_trades_daily=20))

        await market_scanner.scan_once(db)
        # scanner-emitted rows carry no created_at (the 6 seeded ones do)
        emitted = [row for table, row in db.inserted
                   if table == "signals" and "created_at" not in row]
        assert len(emitted) == len(market_scanner.SCAN_ASSETS), (
            "user's max_trades_daily=20 must allow all 5 strong setups "
            "despite 6 signals already emitted today")

    @pytest.mark.asyncio
    async def test_no_duplicate_signal_while_pending_exists(self, monkeypatch):
        """Regression (2026-09-04): the scanner re-emitted the same strong setup
        every cycle (~4 min) because a strong regime persists for hours. The
        auto-trader fired each duplicate and the account stacked 14 open
        positions on 3 assets. A pending signal for the asset must suppress a
        second one until it is approved/expired."""
        db = FakeDatabase()
        db.insert("signals", {"asset": "EURUSD", "direction": "buy",
                              "confidence": 75.0, "approval": "pending"})

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        await market_scanner.scan_once(db)
        # the seed is the ONLY EURUSD signal row — the scan added none
        eurusd = [r for r in db.rows["signals"] if r["asset"] == "EURUSD"]
        assert len(eurusd) == 1, "pending signal for EURUSD must suppress a duplicate"

    @pytest.mark.asyncio
    async def test_open_position_does_not_stop_signal_generation(self, monkeypatch):
        """Regression (2026-09-04, user request): the first dedup fix also
        suppressed signals for assets with an OPEN position — the page went
        silent because all 3 main assets had open positions. Signals must keep
        generating all day; the auto-trader's open-position gate is what
        prevents duplicate orders, not the scanner."""
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "p1", "asset": "EURUSD", "status": "open"}]})

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        await market_scanner.scan_once(db)
        emitted = [row for table, row in db.inserted
                   if table == "signals" and row["asset"] == "EURUSD"]
        assert emitted, "open position must NOT stop signal generation"

    @pytest.mark.asyncio
    async def test_limit_hit_still_emits_signal(self, monkeypatch):
        """Regression (2026-09-04, user request): hitting the daily limit must
        NOT stop signal generation — limits gate ORDER EXECUTION only. The
        card carries the block reason instead ("ไม่ได้เปิดออเดอร์เพราะถึง
        limit แล้ว")."""
        from datetime import datetime, timezone

        from app.models.schemas import AppSettings

        db = FakeDatabase()
        today = datetime.now(timezone.utc).date().isoformat()
        for i in range(3):  # 3 signals today, limit = 3 → limit hit
            db.insert("signals", {"asset": "GBPUSD", "direction": "buy",
                                  "confidence": 75.0,
                                  "created_at": f"{today}T0{i}:00:00Z",
                                  "approval": "approved"})

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        monkeypatch.setattr(market_scanner, "get_app_settings",
                            lambda _db: AppSettings(max_trades_daily=3))
        await market_scanner.scan_once(db)
        emitted = [row for table, row in db.inserted
                   if table == "signals" and "created_at" not in row]
        assert len(emitted) == len(market_scanner.SCAN_ASSETS), (
            "daily limit reached must not stop signal generation")

    @pytest.mark.asyncio
    async def test_gold_min_confidence_gates_signal_generation(self, monkeypatch):
        """Min Confidence (gold) applies to signal generation: with the gold
        threshold at 90, a strong XAUUSD setup scoring 75 must NOT emit a
        signal while the same setup on EURUSD (base 70) still does."""
        from app.models.schemas import AppSettings

        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)  # score ~75

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        monkeypatch.setattr(market_scanner, "get_app_settings",
                            lambda _db: AppSettings(min_confidence=70.0,
                                                    min_confidence_gold=90.0))
        await market_scanner.scan_once(db)
        emitted = [row for table, row in db.inserted
                   if table == "signals" and "created_at" not in row]
        assets = {row["asset"] for row in emitted}
        assert "XAUUSD" not in assets, (
            "gold setup scoring 75 must be blocked by Min Confidence (gold)=90")
        assert "EURUSD" in assets, (
            "same-strength EURUSD setup must still pass the base threshold 70")

    @pytest.mark.asyncio
    async def test_gold_min_confidence_lower_emits_gold_signal(self, monkeypatch):
        """A LOWER gold threshold must let weak gold setups through while the
        base threshold still blocks the same setup on FX pairs."""
        from app.models.schemas import AppSettings

        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return choppy_snapshot(asset, news_sentiment)  # weak setup

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        monkeypatch.setattr(market_scanner, "get_app_settings",
                            lambda _db: AppSettings(min_confidence=70.0,
                                                    min_confidence_gold=30.0))
        await market_scanner.scan_once(db)
        emitted = [row for table, row in db.inserted
                   if table == "signals" and "created_at" not in row]
        assets = {row["asset"] for row in emitted}
        assert "XAUUSD" in assets, (
            "weak gold setup must emit when Min Confidence (gold)=30")
        assert "EURUSD" not in assets, (
            "same weak setup on EURUSD must stay blocked by base threshold 70")

    @pytest.mark.asyncio
    async def test_gold_min_confidence_unset_uses_base(self, monkeypatch):
        """No gold override → behaviour identical to before the feature."""
        from app.models.schemas import AppSettings

        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        monkeypatch.setattr(market_scanner, "get_app_settings",
                            lambda _db: AppSettings(min_confidence=70.0))
        await market_scanner.scan_once(db)
        emitted = [row for table, row in db.inserted
                   if table == "signals" and "created_at" not in row]
        assets = {row["asset"] for row in emitted}
        assert assets == set(market_scanner.SCAN_ASSETS), (
            "without a gold override every strong setup emits as before")

    @pytest.mark.asyncio
    async def test_market_closed_generates_no_signals(self, monkeypatch):
        """Weekend (Sat, or Sun before 21:00 UTC, or Fri after 21:00 UTC) →
        no signals: entries would pin Friday's close all weekend (the
        "ราคาเก่า" complaint)."""
        from datetime import datetime, timezone

        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        # Saturday 2026-09-05 12:00 UTC
        monkeypatch.setattr(market_scanner, "_market_closed",
                            lambda now=None: True)
        await market_scanner.scan_once(db)
        assert not [row for table, row in db.inserted if table == "signals"]
        assert [row for table, row in db.inserted if table == "market_analysis"]

    def test_market_closed_boundaries(self):
        """Unit-check the weekend window: Fri 21:00 UTC → Sun 21:00 UTC.

        Calls the SHARED helper directly — the scanner's _market_closed is
        monkeypatched to False by the autouse _market_open fixture.
        """
        from app.models.schemas import is_market_closed
        from datetime import datetime, timezone
        # Fri 2026-09-04 20:59 UTC → open; 21:00 UTC → closed
        assert is_market_closed(
            datetime(2026, 9, 4, 20, 59, tzinfo=timezone.utc)) is False
        assert is_market_closed(
            datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc)) is True
        # Sat any hour → closed
        assert is_market_closed(
            datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)) is True
        # Sun 20:59 UTC → closed; 21:00 UTC → open again
        assert is_market_closed(
            datetime(2026, 9, 6, 20, 59, tzinfo=timezone.utc)) is True
        assert is_market_closed(
            datetime(2026, 9, 6, 21, 0, tzinfo=timezone.utc)) is False

    @pytest.mark.asyncio
    async def test_signal_entry_anchored_to_live_spot(self, monkeypatch):
        """Regression (2026-09-04, "ราคาเก่า" on signals): ind.price comes from
        fetch_all_snapshots (Frankfurter daily ECB closes + TwelveData gold) —
        ONE close per business day — so every card created that day carried the
        same stale price. The scanner must re-anchor entry/SL/TP at the live
        intraday spot price before persisting."""
        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)  # price=100.0

        async def live_spot(assets, **_kw):
            return {a: 102.5 for a in assets}, {}

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        monkeypatch.setattr(market_scanner.quotes, "fetch_spot_prices", live_spot)
        await market_scanner.scan_once(db)
        emitted = [row for table, row in db.inserted
                   if table == "signals" and "created_at" not in row]
        assert emitted, "strong setup must emit"
        for row in emitted:
            assert row["entry"] == 102.5, (
                "entry must be re-anchored at the live spot price, not the "
                "daily-close snapshot price")
            # SL/TP shift proportionally with the entry (same distances)
            assert row["stop_loss"] < row["entry"] < row["take_profit"]

    @pytest.mark.asyncio
    async def test_signal_entry_keeps_snapshot_price_when_spot_fails(self, monkeypatch):
        """Spot feed failure must NOT zero/garbage the entry — fall back to the
        snapshot price (better than nothing) and still emit."""
        db = FakeDatabase()

        async def snap(asset, news_sentiment=0.0):
            return strong_snapshot(asset, news_sentiment)  # price=100.0

        async def dead_spot(assets, **_kw):
            raise RuntimeError("feed down")

        monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
        monkeypatch.setattr(market_scanner.quotes, "fetch_spot_prices", dead_spot)
        await market_scanner.scan_once(db)
        emitted = [row for table, row in db.inserted
                   if table == "signals" and "created_at" not in row]
        assert emitted
        for row in emitted:
            assert row["entry"] == 100.0, (
                "spot failure must keep the snapshot price as entry")

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
        # nothing persisted except the lifecycle log rows (signal_log logs
        # the 'created' attempt even when the signals insert silently fails)
        assert [row for table, row in db.inserted if table != "signal_logs"] == []

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


# ---------------------------------------------------------------------------
# Position guard — live marks beat the (possibly frozen) broker book
# ---------------------------------------------------------------------------
class _AsyncList:
    """Awaitable list — mirrors the async Broker.all_positions contract."""

    def __init__(self, items):
        self._items = items

    def __await__(self):
        async def _coro():
            return self._items
        return _coro().__await__()


class _AsyncFloat:
    """Awaitable float — mirrors the async Broker.mark_price/quote contract."""

    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _coro():
            return self._value
        return _coro().__await__()


class _AsyncClosed:
    ok = True
    message = "closed"

    def __await__(self):
        async def _coro():
            return self
        return _coro().__await__()


class _AsyncClosedWith:
    """Awaitable close result that records the ticket (close spy)."""

    def __init__(self, sink: list, ticket: str):
        self._sink, self._ticket = sink, ticket

    def __await__(self):
        async def _coro():
            self._sink.append(self._ticket)
            return _AsyncClosed()
        return _coro().__await__()


class TestPositionGuard:
    @pytest.fixture(autouse=True)
    def _no_network_spot(self, monkeypatch):
        async def fake_spot(assets, **_kw):
            return {a: 1.2500 for a in assets}, {}
        monkeypatch.setattr(position_guard.quotes, "fetch_spot_prices", fake_spot)

    def _broker_with(self, asset="EURUSD", entry=1.1000, sl=None, tp=None):
        from app.integrations.brokers import Position
        broker = SimpleNamespace()
        broker._positions = {"T1": Position(
            ticket="T1", user_id="u1", asset=asset, direction="BUY",
            volume=0.01, entry_price=entry, stop_loss=sl, take_profit=tp,
            current_price=entry)}  # rehydrate seeds current_price == entry
        broker.all_positions = lambda: _AsyncList(list(broker._positions.values()))
        broker.mark_price = lambda ticket: _AsyncFloat(broker._positions.get(
            ticket, Position(ticket="", user_id="", asset="", direction="BUY",
                             volume=0, entry_price=0)).current_price)
        broker.quote = lambda asset: _AsyncFloat(0.0)
        broker.close_position = lambda ticket: _AsyncClosed()
        return broker

    @pytest.mark.asyncio
    async def test_live_mark_beats_frozen_book(self, monkeypatch):
        """Regression (2026-09-04, "ราคาเก่า" on monitor): after a deploy the
        rehydrated book pins current_price == entry, and the monitor's
        mark_for preferred ticket marks → every position showed uPnL 0.00
        while the feed was healthy. The guard must mark positions at the
        LIVE feed price, not the frozen book value."""
        from app.workers import position_guard

        class Notifier:
            async def notify(self, *a, **k):
                return None

        broker = self._broker_with(entry=1.1000)  # book frozen at entry
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "p1", "ticket": "T1", "asset": "EURUSD", "status": "open",
             "direction": "buy", "volume": 0.01, "entry_price": 1.1000}]})
        summary = await position_guard.guard_once(
            db, broker, Notifier())
        assert summary["checked"] == 1
        # live spot 1.2500 (fixture) must overwrite the frozen book value
        assert broker._positions["T1"].current_price == 1.2500

    @pytest.mark.asyncio
    async def test_tp_breach_closes_at_live_price(self):
        """The original guard bug: TP already breached but the book never
        ticked → position sat open forever. With live marks the TP fires."""
        from app.workers import position_guard

        closed: list[str] = []

        class Notifier:
            async def notify(self, *a, **k):
                return None

        broker = self._broker_with(entry=1.1000, tp=1.2000)
        broker.close_position = lambda ticket: _AsyncClosedWith(closed, ticket)
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "p1", "ticket": "T1", "asset": "EURUSD", "status": "open",
             "direction": "buy", "volume": 0.01, "entry_price": 1.1000,
             "take_profit": 1.2000}]})
        summary = await position_guard.guard_once(db, broker, Notifier())
        assert summary["closed"] == 1 and closed == ["T1"]

    @pytest.mark.asyncio
    async def test_rehydrate_seeds_entry_price(self):
        """Rehydrate rebuilds the book from open paper_trades rows with
        current_price == entry (the guard's live marks then take over)."""
        from app.workers import position_guard

        class BookBroker:
            def __init__(self):
                self._positions = {}
                self._seq = 0

        db = FakeDatabase(rows={"paper_trades": [
            {"id": "p1", "ticket": "PAPER-000007", "asset": "EURUSD",
             "status": "open", "direction": "buy", "volume": 0.01,
             "entry_price": 1.1615, "user_id": "u1"}]})
        broker = BookBroker()
        restored = await position_guard.rehydrate_book(db, broker)
        assert restored == 1
        pos = broker._positions["PAPER-000007"]
        assert pos.entry_price == 1.1615
        assert pos.current_price == 1.1615  # seeded at entry; guard ticks it
        assert broker._seq == 7  # sequence walked past restored tickets


# ---------------------------------------------------------------------------
# Position guard — breakeven / trailing / partial close management
# ---------------------------------------------------------------------------
class _AsyncModifySL:
    """Awaitable modify_stop_loss that records the new SL (spy)."""

    def __init__(self, sink: list, value: float):
        self._sink, self._value = sink, value

    def __await__(self):
        async def _coro():
            self._sink.append(self._value)
            from app.integrations.brokers import OrderResult
            return OrderResult(ok=True, message="SL moved")
        return _coro().__await__()


class _AsyncPartial:
    """Awaitable partial_close that records the volume (spy)."""

    def __init__(self, sink: list, value: float):
        self._sink, self._value = sink, value

    def __await__(self):
        async def _coro():
            self._sink.append(self._value)
            from app.integrations.brokers import OrderResult
            return OrderResult(ok=True, message="closed 0.01, 0.01 remain")
        return _coro().__await__()


class TestPositionGuardManagement:
    """Breakeven / trailing / partial-close behavior of the guard."""

    @pytest.fixture(autouse=True)
    def _no_network_spot(self, monkeypatch):
        async def fake_spot(assets, **_kw):
            return {a: 1.2500 for a in assets}, {}
        monkeypatch.setattr(position_guard.quotes, "fetch_spot_prices", fake_spot)

    def _broker(self, entry=1.1000, sl=1.0900, tp=None, volume=0.02):
        from app.integrations.brokers import Position
        broker = SimpleNamespace()
        broker._positions = {"T1": Position(
            ticket="T1", user_id="u1", asset="EURUSD", direction="BUY",
            volume=volume, entry_price=entry, stop_loss=sl, take_profit=tp,
            current_price=entry)}
        broker.all_positions = lambda: _AsyncList(list(broker._positions.values()))
        broker.mark_price = lambda ticket: _AsyncFloat(broker._positions.get(
            ticket, Position(ticket="", user_id="", asset="", direction="BUY",
                             volume=0, entry_price=0)).current_price)
        broker.quote = lambda asset: _AsyncFloat(0.0)
        broker.close_position = lambda ticket: _AsyncClosed()
        return broker

    def _db(self, **overrides):
        row = {"id": "p1", "ticket": "T1", "asset": "EURUSD", "status": "open",
               "direction": "buy", "volume": 0.02, "entry_price": 1.1000,
               "stop_loss": 1.0900, "partial_done": False}
        row.update(overrides)
        return FakeDatabase(rows={"paper_trades": [row]})

    def _settings(self, **overrides):
        from app.models.schemas import AppSettings
        s = AppSettings()
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    @pytest.mark.asyncio
    async def test_breakeven_moves_sl_to_entry(self, monkeypatch):
        """Profit ≥ breakeven_trigger_r × R → SL moves to entry."""
        from app.workers import position_guard
        # R distance = |1.1000-1.0900| = 0.0100; live 1.2500 → 15R profit
        moved: list[float] = []
        broker = self._broker()
        broker.modify_stop_loss = lambda ticket, sl: _AsyncModifySL(moved, sl)
        db = self._db()
        summary = await position_guard.guard_once(
            db, broker, _SilentNotifier(),
            settings=self._settings(breakeven_trigger_r=1.0, trail_atr_mult=0))
        assert summary["moved_sl"] == 1
        assert moved == [pytest.approx(1.1000)]

    @pytest.mark.asyncio
    async def test_trailing_extends_beyond_breakeven(self, monkeypatch):
        """With trail_atr_mult > 0 the SL trails price − mult×ATR (≥ entry)."""
        from app.workers import position_guard
        moved: list[float] = []
        broker = self._broker()
        broker.modify_stop_loss = lambda ticket, sl: _AsyncModifySL(moved, sl)
        db = self._db()
        await position_guard.guard_once(
            db, broker, _SilentNotifier(),
            settings=self._settings(breakeven_trigger_r=1.0, trail_atr_mult=2.0))
        # ATR proxy = 0.2 × R distance = 0.002; trail = 1.2500 − 2×0.002 = 1.2460
        assert moved and moved[0] == pytest.approx(1.2460, abs=1e-6)
        assert moved[0] > 1.1000  # strictly better than breakeven

    @pytest.mark.asyncio
    async def test_partial_close_fires_once(self, monkeypatch):
        """partial_close_pct > 0 + profit ≥ partial_trigger_r → one partial."""
        from app.workers import position_guard
        partials: list[float] = []
        broker = self._broker(volume=0.04)
        broker.partial_close = lambda ticket, vol: _AsyncPartial(partials, vol)
        db = self._db(volume=0.04)
        summary = await position_guard.guard_once(
            db, broker, _SilentNotifier(),
            settings=self._settings(partial_close_pct=50, partial_trigger_r=1.0,
                                    breakeven_trigger_r=0, trail_atr_mult=0))
        assert summary["partial_closed"] == 1
        assert partials == [pytest.approx(0.02)]  # 50% of 0.04
        # journal row marked so it never fires twice
        assert db.rows["paper_trades"][0]["partial_done"] is True

    @pytest.mark.asyncio
    async def test_partial_close_not_repeated(self, monkeypatch):
        """partial_done=True (seeded by rehydrate after a restart) → no second
        partial close."""
        from app.workers import position_guard
        partials: list[float] = []
        broker = self._broker(volume=0.04)
        broker._positions["T1"].partial_done = True  # what rehydrate seeds
        broker.partial_close = lambda ticket, vol: _AsyncPartial(partials, vol)
        db = self._db(volume=0.04, partial_done=True)
        summary = await position_guard.guard_once(
            db, broker, _SilentNotifier(),
            settings=self._settings(partial_close_pct=50, partial_trigger_r=1.0,
                                    breakeven_trigger_r=0, trail_atr_mult=0))
        assert summary["partial_closed"] == 0
        assert partials == []

    @pytest.mark.asyncio
    async def test_no_management_when_profit_below_trigger(self, monkeypatch):
        """Profit < trigger → SL untouched, no partial."""
        from app.workers import position_guard
        moved: list[float] = []
        partials: list[float] = []
        broker = self._broker()
        broker.modify_stop_loss = lambda ticket, sl: _AsyncModifySL(moved, sl)
        broker.partial_close = lambda ticket, vol: _AsyncPartial(partials, vol)
        db = self._db()
        # live 1.2500 is 15R in profit — force a small profit instead
        async def small_spot(assets, **_kw):
            return {a: 1.1040 for a in assets}, {}  # 4R... still > 1R
        monkeypatch.setattr(position_guard.quotes, "fetch_spot_prices", small_spot)
        await position_guard.guard_once(
            db, broker, _SilentNotifier(),
            settings=self._settings(breakeven_trigger_r=5.0, trail_atr_mult=0,
                                    partial_close_pct=50, partial_trigger_r=5.0))
        assert moved == [] and partials == []

    @pytest.mark.asyncio
    async def test_sl_never_moves_backwards(self, monkeypatch):
        """A second pass must not move the SL back toward the entry."""
        from app.workers import position_guard
        moved: list[float] = []
        broker = self._broker()
        broker.modify_stop_loss = lambda ticket, sl: _AsyncModifySL(moved, sl)
        db = self._db()
        settings = self._settings(breakeven_trigger_r=1.0, trail_atr_mult=2.0)
        await position_guard.guard_once(db, broker, _SilentNotifier(),
                                        settings=settings)
        first = moved[0]
        # second pass: SL already at first — must not move to a worse value
        broker._positions["T1"].stop_loss = first
        await position_guard.guard_once(db, broker, _SilentNotifier(),
                                        settings=settings)
        assert all(m >= first - 1e-9 for m in moved)


# ---------------------------------------------------------------------------
# Portfolio monitor — breach → pause + notify + equity snapshots
# ---------------------------------------------------------------------------
class _SilentNotifier:
    """Mimics NotificationService.notify: queues a notifications row (the
    daily digest's dedup marker) and records what was sent."""

    def __init__(self, db=None):
        self.db = db
        self.sent: list[tuple[str, str, str]] = []

    async def notify(self, user_id, ntype, message, **_kw):
        self.sent.append((user_id, ntype, message))
        if self.db is not None:
            # created_at mimics the Supabase default now() — the digest dedup
            # reads it back to decide "already sent today"
            self.db.insert("notifications", {
                "user_id": user_id, "channel": "line", "type": ntype,
                "message": message, "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        return None


class TestPortfolioMonitor:
    def _db(self, closed_pnl=0.0, open_rows=0):
        rows = []
        if closed_pnl:
            rows.append({"id": "c1", "status": "closed", "pnl": closed_pnl,
                         "closed_at": datetime.now(timezone.utc).isoformat()})
        for i in range(open_rows):
            rows.append({"id": f"o{i}", "status": "open", "pnl": None,
                         "asset": "EURUSD", "direction": "buy", "volume": 0.01,
                         "entry_price": 1.1000, "stop_loss": 1.0900})
        return FakeDatabase(rows={"paper_trades": rows})

    def test_equity_includes_realized_pnl(self):
        from app.workers import portfolio_monitor
        db = self._db(closed_pnl=250.0)
        assert portfolio_monitor._equity(db, 10_000.0) == pytest.approx(10_250.0)

    def test_equity_snapshot_written_once_per_day(self):
        from app.workers import portfolio_monitor
        db = self._db()
        assert portfolio_monitor._write_equity_snapshot(db, "demo", 10_000.0) is True
        assert len(db.rows["equity_snapshots"]) == 1
        # same day again → update, not a second row
        assert portfolio_monitor._write_equity_snapshot(db, "demo", 10_100.0) is False
        assert len(db.rows["equity_snapshots"]) == 1
        assert db.rows["equity_snapshots"][0]["equity"] == pytest.approx(10_100.0)

    @pytest.mark.asyncio
    async def test_breach_pauses_and_notifies(self, monkeypatch):
        """Limit breach → set_pause(True) + risk_warning notify (the dead-code
        bug fix: previously the alert was built but never sent)."""
        from app.workers import portfolio_monitor
        from app.services import execution

        db = self._db(closed_pnl=-950.0)  # -9.5% of 10k → breaches 8% monthly
        called = {}

        def fake_set_pause(_db, paused, reason):
            called["paused"] = paused
            called["reason"] = reason
            return SimpleNamespace(paused=paused, reason=reason)

        monkeypatch.setattr(execution, "set_pause", fake_set_pause)
        notifier = _SilentNotifier()
        # production runs monitor_once via asyncio.to_thread (sync context →
        # the asyncio.run notify path completes before returning)
        out = await asyncio.to_thread(
            portfolio_monitor.monitor_once, db, None, notifier)
        assert out["breach"] is True
        assert called.get("paused") is True
        assert "risk engine" in (called.get("reason") or "")
        assert any(t == "risk_warning" for _, t, _ in notifier.sent)
        # audit trail row written
        assert any(t == "risk_events" for t, _ in db.inserted)

    @pytest.mark.asyncio
    async def test_no_breach_no_pause(self, monkeypatch):
        from app.workers import portfolio_monitor
        from app.services import execution

        db = self._db(closed_pnl=150.0)
        called = {}

        def fake_set_pause(_db, paused, reason):
            called["paused"] = paused
            return SimpleNamespace(paused=paused, reason=reason)

        monkeypatch.setattr(execution, "set_pause", fake_set_pause)
        out = await asyncio.to_thread(
            portfolio_monitor.monitor_once, db, None, _SilentNotifier())
        assert out["breach"] is False
        assert called == {}
        # equity snapshot still written on healthy cycles
        assert any(t == "equity_snapshots" for t, _ in db.inserted)


# ---------------------------------------------------------------------------
# Calendar sync — populates the economic_calendar table for the news gate
# ---------------------------------------------------------------------------
class TestCalendarSync:
    def test_build_upcoming_events_deterministic(self):
        from app.workers import calendar_sync
        # 2026-09-05 is a Saturday → first Friday Sept = 4th (past), next NFP
        # = Oct 2nd; CPI day_13 → Sep 13; FOMC day_18 → Sep 18
        now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        events = calendar_sync.build_upcoming_events(now)
        names = [e["event"] for e in events]
        assert "CPI" in names and "FOMC" in names
        assert all(e["impact"] == "high" for e in events)
        assert all(e["event_time"] > now.isoformat() for e in events)

    def test_sync_inserts_and_dedups(self):
        from app.workers import calendar_sync
        db = FakeDatabase()
        out1 = calendar_sync.sync_once(db)
        assert out1["inserted"] >= 1
        assert any(t == "economic_calendar" for t, _ in db.inserted)
        # second run → nothing new (dedup by event+event_time)
        out2 = calendar_sync.sync_once(db)
        assert out2["inserted"] == 0

    def test_sync_never_raises_on_db_failure(self):
        from app.workers import calendar_sync
        db = FakeDatabase(fail_tables={"economic_calendar"})
        out = calendar_sync.sync_once(db)  # must not raise
        assert "inserted" in out


# ---------------------------------------------------------------------------
# Daily digest — once per UTC day, idempotent
# ---------------------------------------------------------------------------
class TestDailyDigest:
    def _db(self):
        now = datetime.now(timezone.utc)
        return FakeDatabase(rows={"paper_trades": [
            {"id": "c1", "status": "closed", "pnl": 120.0,
             "closed_at": (now - timedelta(days=1)).isoformat()},
            {"id": "c2", "status": "closed", "pnl": -50.0,
             "closed_at": (now - timedelta(days=1)).isoformat()},
            {"id": "o1", "status": "open", "pnl": None, "asset": "EURUSD",
             "direction": "buy", "volume": 0.01, "entry_price": 1.1000},
        ]})

    def test_build_digest_contains_sections(self):
        from app.workers import daily_digest
        db = self._db()
        msg = daily_digest.build_digest(db, 10_070.0, 10_000.0)
        assert "Daily Digest" in msg
        assert "+70.00" in msg  # yesterday PnL 120 - 50
        assert "ไม้เปิดค้าง" in msg

    @pytest.mark.asyncio
    async def test_send_once_dedups_per_day(self):
        from app.workers import daily_digest
        db = self._db()
        notifier = _SilentNotifier(db)
        out1 = await daily_digest.send_digest_once(db, notifier)
        assert out1["sent"] is True
        assert any(t == "daily_digest" for _, t, _ in notifier.sent)
        # the queued notifications row IS the dedup marker
        assert any(t == "notifications" for t, _ in db.inserted)
        # second call same day → skipped (dedup reads the notifications table)
        out2 = await daily_digest.send_digest_once(db, notifier)
        assert out2["sent"] is False
        assert out2["reason"] == "already sent today"

    @pytest.mark.asyncio
    async def test_send_once_db_unavailable(self):
        from app.workers import daily_digest

        class NoDb:
            available = False

            def select(self, *a, **k):
                return []

        out = await daily_digest.send_digest_once(NoDb(), _SilentNotifier())
        assert out["sent"] is False
