"""Tests for the signal lifecycle log (signal_log service + endpoint + hooks).

Covers: log_event insert/skip/swallow/unknown-event, TTL purge (per-row
fallback path — FakeDatabase has no delete_before), summary aggregation,
GET /api/system/signal-logs, and the lifecycle hooks wired into the scanner
(created), execution (order_opened / order_blocked / expired), approve route
(rejected) and position guard (closed).

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_signal_log.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import signal_log
from tests.test_workers import FakeDatabase


def _row(days_old: float, event: str = "created", asset: str = "EURUSD") -> dict:
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {"id": f"row-{days_old}-{event}-{asset}", "created_at": created,
            "signal_id": f"sig-{asset}", "asset": asset, "direction": "buy",
            "event": event, "confidence": 80.0, "entry": 1.1, "source": "scanner",
            "reason": "test row", "ticket": "", "volume": None, "pnl": None,
            "exit_price": None}


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------
class TestLogEvent:
    def test_log_event_inserts_row(self):
        db = FakeDatabase()
        signal_log.log_event(db=db, event="created", signal_id="s1",
                             asset="XAUUSD", direction="BUY", confidence=88.5,
                             entry=2400.0, stop_loss=2350.0,
                             take_profit=2500.0, source="scanner",
                             reason="EMA cross + strong regime")
        assert len(db.inserted) == 1
        table, row = db.inserted[0]
        assert table == "signal_logs"
        assert row["event"] == "created"
        assert row["direction"] == "buy"  # lowercased
        assert row["confidence"] == 88.5
        assert row["asset"] == "XAUUSD"

    def test_log_event_skips_when_db_none(self):
        signal_log.log_event(db=None, event="created")  # must not raise

    def test_log_event_skips_when_db_unavailable(self):
        class Dead:
            available = False
        signal_log.log_event(db=Dead(), event="created")  # must not raise

    def test_log_event_swallows_db_errors(self):
        class Boom:
            available = True

            def insert(self, *a, **k):
                raise RuntimeError("db down")
        signal_log.log_event(db=Boom(), event="order_opened")  # must not raise

    def test_unknown_event_falls_back_to_created(self):
        db = FakeDatabase()
        signal_log.log_event(db=db, event="nonsense")
        assert db.inserted[0][1]["event"] == "created"

    def test_reason_truncated_to_500(self):
        db = FakeDatabase()
        signal_log.log_event(db=db, event="created", reason="x" * 900)
        assert len(db.inserted[0][1]["reason"]) == 500


# ---------------------------------------------------------------------------
# TTL purge — FakeDatabase has no delete_before → per-row fallback path
# ---------------------------------------------------------------------------
class TestPurge:
    def test_purge_deletes_rows_older_than_7_days(self):
        db = FakeDatabase(rows={"signal_logs": [
            _row(8), _row(10), _row(3)]})
        deleted = signal_log.purge_old_logs(db, force=True)
        assert deleted == 2
        assert len(db.rows["signal_logs"]) == 1

    def test_purge_keeps_rows_within_7_days(self):
        db = FakeDatabase(rows={"signal_logs": [_row(6.9)]})
        assert signal_log.purge_old_logs(db, force=True) == 0
        assert len(db.rows["signal_logs"]) == 1

    def test_purge_throttled(self):
        db = FakeDatabase(rows={"signal_logs": [_row(10)]})
        signal_log.purge_old_logs(db, force=True)
        assert signal_log.purge_old_logs(db) == 0  # skipped (no force)

    def test_purge_uses_delete_before_when_available(self):
        deleted_calls: list[tuple] = []

        class BulkDb(FakeDatabase):
            def delete_before(self, table, column, cutoff):
                deleted_calls.append((table, column))
                return 5

        db = BulkDb(rows={"signal_logs": [_row(10)]})
        assert signal_log.purge_old_logs(db, force=True) == 5
        assert deleted_calls == [("signal_logs", "created_at")]

    def test_purge_never_raises(self):
        class Boom:
            available = True

            def delete_before(self, *a):
                raise RuntimeError("boom")
        assert signal_log.purge_old_logs(Boom(), force=True) == 0


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
class TestSummary:
    def test_summary_aggregates_by_event(self):
        db = FakeDatabase(rows={"signal_logs": [
            _row(0.1, event="created"),
            _row(0.1, event="order_opened"),
            _row(0.1, event="order_blocked"),
            _row(0.1, event="order_blocked"),
            _row(0.1, event="expired"),
            _row(0.1, event="rejected"),
            _row(0.1, event="closed", asset="XAUUSD"),
        ]})
        s = signal_log.summary(db)
        assert s["total"] == 7
        assert s["opened"] == 1 and s["blocked"] == 2
        assert s["expired"] == 1 and s["rejected"] == 1 and s["closed"] == 1
        assert s["by_event"]["created"] == 1
        assert s["by_asset"]["XAUUSD"] == 1

    def test_summary_ignores_rows_older_than_7_days(self):
        db = FakeDatabase(rows={"signal_logs": [_row(9)]})
        assert signal_log.summary(db)["total"] == 0

    def test_summary_empty_db(self):
        s = signal_log.summary(FakeDatabase())
        assert s["total"] == 0 and s["opened"] == 0


# ---------------------------------------------------------------------------
# GET /api/system/signal-logs
# ---------------------------------------------------------------------------
class TestSignalLogsEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_returns_rows_and_summary(self):
        from app.main import app
        from tests.test_api_routes import call, set_state

        db = FakeDatabase(rows={"signal_logs": [
            _row(0.01, event="order_opened", asset="XAUUSD"),
            _row(0.01, event="created"),
        ]})
        set_state(db)
        res = await call("GET", "/api/system/signal-logs")
        assert res.status_code == 200
        body = res.json()
        assert body["verdict"] == "ok"
        assert len(body["logs"]) == 2
        assert body["summary"]["total"] == 2
        assert body["summary"]["opened"] == 1
        assert body["ttl_days"] == 7

    @pytest.mark.asyncio
    async def test_endpoint_unavailable_db(self):
        from app.main import app
        from tests.test_api_routes import call

        class Dead:
            available = False
            init_error = "no env"
        app.state.db = Dead()
        res = await call("GET", "/api/system/signal-logs")
        assert res.status_code == 200
        assert res.json()["verdict"] == "fail"


# ---------------------------------------------------------------------------
# Lifecycle hooks — scanner 'created', execution open/block/expire,
# approve reject, guard close
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scanner_logs_created_event(monkeypatch):
    from app.workers import market_scanner
    from tests.test_workers import strong_snapshot

    db = FakeDatabase()

    async def snap(asset, news_sentiment=0.0):
        return strong_snapshot(asset, news_sentiment)

    monkeypatch.setattr(market_scanner, "_snapshot_for", snap)
    await market_scanner.scan_once(db)
    created = [row for table, row in db.inserted
               if table == "signal_logs" and row.get("event") == "created"]
    assert created, "scanner must log a 'created' event for each emitted signal"
    assert all(row["source"] == "scanner" for row in created)
    assert all(row.get("reason") for row in created)


@pytest.mark.asyncio
async def test_execute_signal_logs_opened_and_blocked():
    from app.services import execution
    from tests.test_auto_trader import clean_settings, db_with_client

    class OkBroker:
        async def place_order(self, order):
            return SimpleNamespace(ok=True, broker_order_id="TCK-1", message="ok")

    class Silent:
        async def notify(self, *a, **k):
            return None

    settings = clean_settings()
    # opened path
    db = db_with_client()
    report = await execution.execute_signal(
        db, OkBroker(), None, settings,
        user_id="demo", asset="EURUSD", direction="BUY", entry=1.1,
        stop_loss=1.09, take_profit=1.12, confidence=90.0, opportunity=90.0,
        signal_id="s1", source="approved")
    assert report.allowed
    opened = [row for table, row in db.inserted
              if table == "signal_logs" and row.get("event") == "order_opened"]
    assert len(opened) == 1
    assert opened[0]["ticket"] == "TCK-1"

    # blocked path (paused)
    execution.set_pause(db, True, "test pause")
    db.inserted.clear()
    report = await execution.execute_signal(
        db, OkBroker(), Silent(), settings,
        user_id="demo", asset="EURUSD", direction="BUY", entry=1.1,
        stop_loss=1.09, take_profit=1.12, confidence=90.0, opportunity=90.0,
        signal_id="s1", source="approved")
    assert not report.allowed
    blocked = [row for table, row in db.inserted
               if table == "signal_logs" and row.get("event") == "order_blocked"]
    assert len(blocked) == 1
    assert "pause" in blocked[0]["reason"].lower()


@pytest.mark.asyncio
async def test_expire_stale_logs_expired_event():
    from app.services import execution

    db = FakeDatabase(rows={"signals": [{
        "id": "s-old", "asset": "EURUSD", "direction": "buy",
        "confidence": 80.0, "entry": 1.1, "approval": "pending",
        "created_at": (datetime.now(timezone.utc)
                       - timedelta(minutes=45)).isoformat()}]})
    n = execution.expire_stale_pending_signals(db)
    assert n == 1
    expired = [row for table, row in db.inserted
               if table == "signal_logs" and row.get("event") == "expired"]
    assert len(expired) == 1
    assert expired[0]["signal_id"] == "s-old"
    assert db.rows["signals"][0]["approval"] == "expired"


@pytest.mark.asyncio
async def test_approve_reject_logs_rejected_event():
    from app.main import app
    from tests.test_api_routes import call, set_state

    db = FakeDatabase(rows={"signals": [{
        "id": "s-rj", "asset": "XAUUSD", "direction": "buy",
        "confidence": 85.0, "entry": 2400.0, "approval": "pending"}]})
    set_state(db)
    res = await call("POST", "/api/signals/approve",
                     {"signal_id": "s-rj", "approve": False})
    assert res.status_code == 200
    rejected = [row for table, row in db.inserted
                if table == "signal_logs" and row.get("event") == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["asset"] == "XAUUSD"
    assert rejected[0]["source"] == "user"


@pytest.mark.asyncio
async def test_position_guard_logs_closed_event(monkeypatch):
    from app.integrations.brokers import Position
    from app.workers import position_guard

    # Pin the live spot feed so the guard uses OUR exit price (1.121),
    # not a real network quote (same trick test_auto_trader uses).
    registry: dict[str, float] = {}

    async def fake_fetch(assets):
        return ({a: registry[a] for a in assets if a in registry}, {})

    monkeypatch.setattr(position_guard.quotes, "fetch_spot_prices", fake_fetch)
    registry["EURUSD"] = 1.121

    class GuardBroker:
        def __init__(self, pos):
            self.pos = pos

        async def all_positions(self):
            return [self.pos]

        async def close_position(self, ticket):
            return SimpleNamespace(ok=True, broker_order_id=ticket, message="closed")

        async def mark_price(self, ticket):
            return 0.0

        async def quote(self, asset):
            return 0.0

    pos = Position(user_id="demo", ticket="TCK-9", asset="EURUSD",
                   direction="BUY", volume=0.01, entry_price=1.1,
                   stop_loss=1.09, take_profit=1.12, current_price=1.121)
    db = FakeDatabase()
    out = await position_guard.guard_once(db, GuardBroker(pos), None)
    assert out["closed"] == 1
    closed = [row for table, row in db.inserted
              if table == "signal_logs" and row.get("event") == "closed"]
    assert len(closed) == 1
    assert closed[0]["ticket"] == "TCK-9"
    assert closed[0]["exit_price"] == 1.121
    assert "TP" in closed[0]["reason"] or "tp" in closed[0]["reason"]
