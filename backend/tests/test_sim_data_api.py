"""Dataset build + persistence API (Task 4, sim price-data V2).

Background build service with single-worker guard, batch persistence, and
two routes. No live network: provider adapters are monkeypatched.

Run from backend/: D:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_sim_data_api.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.main import app
from app.services import sim_market_data
from app.services.sim_market_data import SimBar
from tests.test_workers import FakeDatabase


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def set_state(db: FakeDatabase) -> None:
    app.state.db = db


async def call(method: str, path: str,
               json_body: dict | None = None) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                base_url="http://test") as client:
        resp = await client.request(method, path, json=json_body)
        return resp.json()


@pytest.fixture
def fake_db() -> FakeDatabase:
    db = FakeDatabase()
    set_state(db)
    return db


@pytest.fixture(autouse=True)
def _clean_build_slot():
    sim_market_data._set_active(None)
    yield
    sim_market_data._set_active(None)
    set_state(FakeDatabase())


class _InlinePool:
    """Run the worker synchronously so the build reaches `done`
    deterministically inside the test (production uses the real pool)."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return None


@pytest.fixture
def inline_worker(monkeypatch):
    monkeypatch.setattr(sim_market_data, "_POOL", _InlinePool())
    monkeypatch.setattr(sim_market_data, "_TD_MIN_GAP_S", 0.0)


def _drift_bars(asset: str, end_ago_days: int, n: int,
                base: float, source: str) -> list[SimBar]:
    """n valid closed daily bars ending `end_ago_days` ago, oldest-first."""
    today = datetime.now(timezone.utc).date()
    out: list[SimBar] = []
    for i in range(n):
        day = today - timedelta(days=end_ago_days + (n - 1 - i))
        close = base + i * 0.0001  # slow drift — never a spike
        out.append(SimBar(asset, day.isoformat(),
                          close - 0.0002, close + 0.0003, close - 0.0004,
                          close, source, None))
    return out


async def fake_bq(asset, client):
    return _drift_bars(asset, end_ago_days=1, n=300,
                       base=1.1000, source="biquote")


async def fake_td(asset, *, years, client):
    return _drift_bars(asset, end_ago_days=301, n=300,
                       base=1.0500, source="twelvedata")


# ---------------------------------------------------------------------------
# Brief Step 1 tests
# ---------------------------------------------------------------------------
class TestDatasetBuildApi:
    @pytest.mark.asyncio
    async def test_build_creates_new_version_and_persists_bars(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        res = await call("POST", "/api/system/sim-data/build",
                         {"assets": ["EURUSD"], "years": 5})
        assert res["ok"] is True
        assert res["status"] == "done"
        assert fake_db.count("sim_price_bars_v2") > 0

    @pytest.mark.asyncio
    async def test_second_build_is_refused_while_active(self, fake_db):
        sim_market_data._set_active("existing")
        res = await call("POST", "/api/system/sim-data/build",
                         {"assets": ["EURUSD"]})
        assert res["ok"] is False

    @pytest.mark.asyncio
    async def test_status_and_dataset_roundtrip(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        built = await call("POST", "/api/system/sim-data/build",
                           {"assets": ["EURUSD"], "years": 5})
        version = built["version"]
        status = await call("GET", f"/api/system/sim-data/{version}")
        assert status["ok"] is True
        assert status["status"] == "complete"
        assert status["stage"] == "done"
        assert status["report"]["complete"] is True

        ds = sim_market_data.load_dataset(fake_db, version)
        assert set(ds) == {"EURUSD"}
        candles = ds["EURUSD"]
        assert len(candles) >= 250
        assert all(c.t > 0 for c in candles)
        assert [c.t for c in candles] == sorted(c.t for c in candles)

    @pytest.mark.asyncio
    async def test_build_fails_when_coverage_insufficient(
            self, fake_db, inline_worker, monkeypatch):
        async def tiny_bq(asset, client):
            return _drift_bars(asset, end_ago_days=1, n=10,
                               base=1.1000, source="biquote")

        async def tiny_td(asset, *, years, client):
            return []

        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", tiny_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            tiny_td)
        res = await call("POST", "/api/system/sim-data/build",
                         {"assets": ["EURUSD"], "years": 5})
        assert res["ok"] is True
        assert res["status"] == "failed"
