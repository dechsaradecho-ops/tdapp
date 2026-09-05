"""Tests for the quote API call log (quote_log service + endpoints).

Covers: masking, URL key-stripping, TTL purge, summary aggregation, the
/api/system/quote-logs endpoint and the /api/system/quote-test button.
Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_quote_log.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.main import app
from app.services import quote_log
from tests.test_workers import FakeDatabase
from tests.test_api_routes import call, set_state


# ---------------------------------------------------------------------------
# Masking + URL hygiene
# ---------------------------------------------------------------------------
class TestMasking:
    def test_mask_key_first_half_only(self):
        key = "0aacb73092285cee6abd420a"
        masked = quote_log.mask_key(key)
        assert masked == key[:len(key) // 2] + "…"
        assert key[len(key) // 2:] not in masked  # second half never stored

    def test_mask_key_empty(self):
        assert quote_log.mask_key("") == ""

    def test_strip_key_from_url(self):
        url = "https://api.twelvedata.com/time_series?symbol=XAU/USD&apikey=SECRET123&interval=1day"
        stripped = quote_log.strip_key_from_url(url)
        assert "SECRET123" not in stripped
        assert "apikey=…" in stripped
        assert "interval=1day" in stripped

    def test_strip_key_url_without_key_untouched(self):
        url = "https://api.frankfurter.dev/v1/2026-01-01..2026-01-31"
        assert quote_log.strip_key_from_url(url) == url


# ---------------------------------------------------------------------------
# TTL purge (7 days)
# ---------------------------------------------------------------------------
def _row(days_old: float, status: str = "success",
         category: str = "forex", provider: str = "exchangerate") -> dict:
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {"id": f"row-{days_old}-{status}-{category}",
            "created_at": created, "status": status, "category": category,
            "provider": provider, "asset": "EURUSD", "url": "https://x",
            "api_key_hint": "", "http_status": 200, "price": 1.1,
            "error": "", "duration_ms": 12}


class TestPurge:
    def test_purge_deletes_rows_older_than_7_days(self):
        db = FakeDatabase(rows={"quote_api_logs": [
            _row(8), _row(10), _row(3)]})
        quote_log.set_db(db)
        quote_log._last_purge = 0.0  # force the purge to run
        deleted = quote_log.purge_old_logs(db, force=True)
        assert deleted == 2
        assert len(db.rows["quote_api_logs"]) == 1

    def test_purge_keeps_rows_within_7_days(self):
        db = FakeDatabase(rows={"quote_api_logs": [_row(6.9)]})
        quote_log.set_db(db)
        deleted = quote_log.purge_old_logs(db, force=True)
        assert deleted == 0
        assert len(db.rows["quote_api_logs"]) == 1

    def test_purge_throttled(self):
        db = FakeDatabase(rows={"quote_api_logs": [_row(10)]})
        quote_log.set_db(db)
        quote_log.purge_old_logs(db, force=True)
        # second call within PURGE_INTERVAL_S is skipped (no force)
        assert quote_log.purge_old_logs(db) == 0

    def test_purge_unavailable_db(self):
        class Dead:
            available = False
        quote_log.set_db(Dead())
        assert quote_log.purge_old_logs(Dead(), force=True) == 0
        quote_log.set_db(None)


# ---------------------------------------------------------------------------
# Summary aggregation (forex vs gold)
# ---------------------------------------------------------------------------
class TestSummary:
    def test_summary_splits_forex_and_gold(self):
        db = FakeDatabase(rows={"quote_api_logs": [
            _row(0.1, status="success", category="forex"),
            _row(0.1, status="error", category="forex"),
            _row(0.1, status="success", category="gold"),
        ]})
        s = quote_log.summary(db)
        assert s["total"] == 3
        assert s["success"] == 2 and s["error"] == 1
        assert s["forex"] == {"total": 2, "success": 1, "error": 1}
        assert s["gold"] == {"total": 1, "success": 1, "error": 0}
        assert s["by_provider"]["exchangerate"]["total"] == 3

    def test_summary_ignores_rows_older_than_7_days(self):
        db = FakeDatabase(rows={"quote_api_logs": [_row(9)]})
        s = quote_log.summary(db)
        assert s["total"] == 0

    def test_summary_empty_db(self):
        s = quote_log.summary(FakeDatabase())
        assert s["total"] == 0

    def test_summary_counts_past_1000_rows(self):
        """Regression: one select(limit=1000) truncated the count — paging
        must keep counting rows beyond the first 1000."""
        db = FakeDatabase(rows={"quote_api_logs": [
            _row(0.1, status="success" if i % 10 else "error")
            for i in range(1250)]})
        s = quote_log.summary(db)
        assert s["total"] == 1250
        assert s["success"] == 1125 and s["error"] == 125


# ---------------------------------------------------------------------------
# log_call — never raises, skips when DB down
# ---------------------------------------------------------------------------
class TestLogCall:
    def test_log_call_inserts_row(self):
        db = FakeDatabase()
        quote_log.set_db(db)
        quote_log.log_call(asset="EURUSD", category="forex",
                           provider="exchangerate", url="https://x",
                           status="success", http_status=200, price=1.16,
                           duration_ms=15, api_key_hint="abc…")
        assert len(db.inserted) == 1
        table, row = db.inserted[0]
        assert table == "quote_api_logs"
        assert row["status"] == "success"
        assert row["price"] == 1.16

    def test_log_call_swallows_errors(self):
        class Boom:
            available = True

            def insert(self, *a, **k):
                raise RuntimeError("db down")
        quote_log.set_db(Boom())
        quote_log.log_call(asset="X", category="forex", provider="p",
                           url="u", status="success")  # must not raise
        quote_log.set_db(None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
class TestQuoteLogEndpoints:
    @pytest.fixture(autouse=True)
    def _clean_quote_db(self):
        quote_log.set_db(None)
        yield
        quote_log.set_db(None)

    @pytest.mark.asyncio
    async def test_quote_logs_endpoint_returns_rows_and_summary(self):
        db = FakeDatabase(rows={"quote_api_logs": [
            _row(0.01, status="success", category="gold"),
            _row(0.01, status="error", category="forex"),
        ]})
        set_state(db)
        quote_log.set_db(db)
        res = await call("GET", "/api/system/quote-logs")
        assert res.status_code == 200
        body = res.json()
        assert body["verdict"] == "ok"
        assert len(body["logs"]) == 2
        assert body["summary"]["total"] == 2
        assert body["summary"]["gold"]["total"] == 1
        assert body["ttl_days"] == 7

    @pytest.mark.asyncio
    async def test_quote_logs_endpoint_unavailable_db(self):
        class Dead:
            available = False
            init_error = "no env"
        app.state.db = Dead()
        res = await call("GET", "/api/system/quote-logs")
        assert res.status_code == 200
        assert res.json()["verdict"] == "fail"

    @pytest.mark.asyncio
    async def test_quote_test_endpoint_forces_fetch(self, monkeypatch):
        set_state(FakeDatabase())
        from app.integrations import quotes

        async def fake_fetch(assets):
            return {"EURUSD": 1.1619}, {"XAUUSD": "XAUUSD: spot feed timeout"}

        monkeypatch.setattr(quotes, "fetch_spot_prices", fake_fetch)
        res = await call("POST", "/api/system/quote-test")
        assert res.status_code == 200
        body = res.json()
        assert body["verdict"] == "ok"
        assert body["prices"]["EURUSD"] == 1.1619
        assert "XAUUSD" in body["failures"]

    @pytest.mark.asyncio
    async def test_quote_test_endpoint_all_fail(self, monkeypatch):
        set_state(FakeDatabase())
        from app.integrations import quotes

        async def dead_fetch(assets):
            return {}, {a: f"{a}: dead" for a in assets}

        monkeypatch.setattr(quotes, "fetch_spot_prices", dead_fetch)
        res = await call("POST", "/api/system/quote-test")
        body = res.json()
        assert body["verdict"] == "fail"
        assert body["prices"] == {}
