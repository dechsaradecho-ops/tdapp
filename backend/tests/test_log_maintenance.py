"""Tests for the log/table maintenance worker (audit item 6).

Fixes: quote_api_logs (66k rows), scheduler_runs and signal_logs were only
trimmed when the user happened to open the page that triggers the purge
inline, and 380 quote failures/week produced no alert at all.

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_log_maintenance.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.workers import log_maintenance
from app.workers import market_scanner
from tests.test_workers import FakeDatabase


def _stamp(days_old: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()


def _stamp_minutes_ago(minutes: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes)).isoformat()


def _old_row(table: str, days_old: float, **extra) -> dict:
    return {"id": f"{table}-{days_old}-{len(str(extra))}",
            "created_at": _stamp(days_old), **extra}


class RecordingNotifier:
    def __init__(self):
        self.sent: list[tuple] = []

    async def notify(self, user_id, ntype, message, critical=None):
        self.sent.append((user_id, ntype, message))
        return True


@pytest.fixture(autouse=True)
def _reset_alert_throttle(monkeypatch):
    """The alert cooldown is a module global — reset it per test."""
    monkeypatch.setattr(log_maintenance, "_last_alert", 0.0)
    monkeypatch.setattr(log_maintenance, "ALERT_COOLDOWN_S", 0.0)


def _full_db() -> FakeDatabase:
    return FakeDatabase(rows={
        "quote_api_logs": [_old_row("quote_api_logs", 8, status="success")],
        "signal_logs": [_old_row("signal_logs", 9, event="created")],
        "scheduler_runs": [_old_row("scheduler_runs", 8, job_id="market_scanner")],
        "market_analysis": [_old_row("market_analysis", 8, asset="EURUSD")],
    })


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_purges_every_ttl_table(self):
        db = _full_db()
        out = await log_maintenance.run_once(db)
        assert out["purged"]["quote_api_logs"] == 1
        assert out["purged"]["signal_logs"] == 1
        assert out["purged"]["scheduler_runs"] == 1
        assert out["purged"]["market_analysis"] == 1
        for table in ("quote_api_logs", "signal_logs", "scheduler_runs",
                      "market_analysis"):
            assert db.rows[table] == [], table

    @pytest.mark.asyncio
    async def test_keeps_fresh_rows(self):
        db = FakeDatabase(rows={
            "quote_api_logs": [{"id": "q1", "created_at": _stamp_minutes_ago(5),
                                "status": "success"}],
            "signal_logs": [_old_row("signal_logs", 0.1)],
            "scheduler_runs": [_old_row("scheduler_runs", 0.1)],
            "market_analysis": [_old_row("market_analysis", 0.1)],
        })
        out = await log_maintenance.run_once(db)
        assert sum(out["purged"].values()) == 0
        assert out["quote_calls_1h"] == 1

    @pytest.mark.asyncio
    async def test_one_broken_table_does_not_stop_the_others(self):
        class Flaky(FakeDatabase):
            def select(self, table, *a, **k):
                if table == "signal_logs":
                    raise RuntimeError("db down")
                return super().select(table, *a, **k)

        db = Flaky(rows={"quote_api_logs": [_old_row("quote_api_logs", 9)],
                         "market_analysis": [_old_row("market_analysis", 9)]})
        out = await log_maintenance.run_once(db)
        assert out["purged"]["quote_api_logs"] == 1

    @pytest.mark.asyncio
    async def test_never_raises_on_dead_db(self):
        class Dead:
            available = False
        out = await log_maintenance.run_once(Dead())
        assert out["purged"]["quote_api_logs"] == 0


class TestQuoteErrorRate:
    def _db(self, ok: int, err: int, age_min: float = 10) -> FakeDatabase:
        rows = [{"id": f"ok-{i}", "created_at": _stamp_minutes_ago(age_min),
                 "status": "success", "category": "forex",
                 "provider": "yahoo", "asset": "EURUSD"}
                for i in range(ok)]
        rows += [{"id": f"err-{i}", "created_at": _stamp_minutes_ago(age_min),
                  "status": "error", "category": "forex",
                  "provider": "yahoo", "asset": "EURUSD"}
                 for i in range(err)]
        return FakeDatabase(rows={"quote_api_logs": rows})

    def test_rate_is_exact(self):
        total, errors, rate = log_maintenance.quote_error_rate(self._db(50, 50))
        assert (total, errors) == (100, 50)
        assert rate == pytest.approx(0.5)

    def test_empty_window_is_zero(self):
        assert log_maintenance.quote_error_rate(FakeDatabase()) == (0, 0, 0.0)

    def test_old_rows_outside_window_ignored(self):
        db = self._db(0, 100, age_min=180)  # 3h ago > 1h window
        total, errors, rate = log_maintenance.quote_error_rate(db)
        assert (total, errors, rate) == (0, 0, 0.0)

    def test_dead_db_is_zero(self):
        class Dead:
            available = False
        assert log_maintenance.quote_error_rate(Dead()) == (0, 0, 0.0)


class TestAlerting:
    @pytest.mark.asyncio
    async def test_alerts_when_error_rate_is_high(self):
        db = FakeDatabase(rows={"quote_api_logs": [
            {"id": f"r{i}", "created_at": _stamp_minutes_ago(5),
             "status": "success" if i < 60 else "error", "category": "forex",
             "provider": "yahoo", "asset": "EURUSD"} for i in range(100)]})
        notifier = RecordingNotifier()
        out = await log_maintenance.run_once(db, notifier)
        assert notifier.sent and notifier.sent[0][1] == "risk_warning"
        assert "40/100" in notifier.sent[0][2]
        assert "alert" in out

    @pytest.mark.asyncio
    async def test_silent_below_the_threshold(self):
        db = FakeDatabase(rows={"quote_api_logs": [
            {"id": f"r{i}", "created_at": _stamp_minutes_ago(5),
             "status": "success" if i < 95 else "error", "category": "forex",
             "provider": "yahoo", "asset": "EURUSD"} for i in range(100)]})
        notifier = RecordingNotifier()
        out = await log_maintenance.run_once(db, notifier)
        assert notifier.sent == []
        assert "alert" not in out

    @pytest.mark.asyncio
    async def test_small_sample_never_alerts(self):
        """A 100%-error hour with only 4 calls is noise, not an outage."""
        db = FakeDatabase(rows={"quote_api_logs": [
            {"id": f"r{i}", "created_at": _stamp_minutes_ago(5),
             "status": "error", "category": "forex",
             "provider": "yahoo", "asset": "EURUSD"} for i in range(4)]})
        notifier = RecordingNotifier()
        await log_maintenance.run_once(db, notifier)
        assert notifier.sent == []

    @pytest.mark.asyncio
    async def test_no_notifier_is_fine(self):
        db = FakeDatabase(rows={"quote_api_logs": [
            {"id": f"r{i}", "created_at": _stamp_minutes_ago(5),
             "status": "error", "category": "forex",
             "provider": "yahoo", "asset": "EURUSD"} for i in range(100)]})
        out = await log_maintenance.run_once(db, None)
        assert "quote_error_rate" in out


class TestMarketAnalysisRetention:
    def test_purge_deletes_old_scanner_rows(self):
        db = FakeDatabase(rows={"market_analysis": [
            _old_row("market_analysis", 8, asset="EURUSD"),
            _old_row("market_analysis", 1, asset="XAUUSD")]})
        assert market_scanner.purge_old_market_analysis(db, force=True) == 1
        assert len(db.rows["market_analysis"]) == 1

    def test_probe_rows_are_never_deleted(self):
        """/api/system/db-check inserts an asset=PROBE row to prove writes."""
        db = FakeDatabase(rows={"market_analysis": [
            {"id": "probe", "created_at": _stamp(30), "asset": "PROBE"}]})
        assert market_scanner.purge_old_market_analysis(db, force=True) == 0
        assert len(db.rows["market_analysis"]) == 1

    def test_throttled_without_force(self, monkeypatch):
        monkeypatch.setattr(market_scanner, "_last_purge", 0.0)
        db = FakeDatabase(rows={"market_analysis": [
            _old_row("market_analysis", 9, asset="EURUSD")]})
        market_scanner.purge_old_market_analysis(db)      # runs, 1 deleted
        db.rows["market_analysis"] = [_old_row("market_analysis", 9,
                                              asset="EURUSD")]
        assert market_scanner.purge_old_market_analysis(db) == 0  # throttled

    def test_uses_bulk_delete_before_when_available(self):
        calls: list[tuple] = []

        class BulkDb(FakeDatabase):
            def delete_before(self, table, column, cutoff):
                calls.append((table, column, cutoff[:10]))
                return 42

        db = BulkDb()
        assert market_scanner.purge_old_market_analysis(db, force=True) == 42
        assert calls and calls[0][:2] == ("market_analysis", "created_at")

    @pytest.mark.asyncio
    async def test_scan_once_triggers_retention(self, monkeypatch):
        """Every cycle trims — the fix is that this no longer depends on the
        user opening a page."""
        from app.workers import market_scanner as ms

        called: list[bool] = []
        monkeypatch.setattr(ms, "purge_old_market_analysis",
                            lambda db, force=False: called.append(True) or 0)
        monkeypatch.setattr(ms, "_scan_assets", lambda settings: [])
        monkeypatch.setattr(ms, "_news_sentiment_by_asset", lambda db: {})
        monkeypatch.setattr(ms, "_calendar_high_impact", lambda db, s: False)
        monkeypatch.setattr(ms, "_market_closed", lambda *a: True)

        async def no_snaps(assets):
            return {}

        async def no_spots(assets):
            return {}, {}

        monkeypatch.setattr(ms.quotes, "fetch_all_snapshots", no_snaps)
        monkeypatch.setattr(ms.quotes, "fetch_spot_prices", no_spots)
        await ms.scan_once(FakeDatabase())
        assert called == [True]
