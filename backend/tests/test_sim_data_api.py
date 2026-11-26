"""Dataset build + persistence API (Task 4, sim price-data V2).

Background build service with single-worker guard, batch persistence, and
two routes. No live network: provider adapters are monkeypatched.

Run from backend/: D:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_sim_data_api.py -v
"""
from __future__ import annotations

import threading
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


class TestDatasetBuildGuard:
    """Finding 1: the active check-and-set must be a single locked CAS."""

    def test_concurrent_starts_only_one_wins(
            self, fake_db, monkeypatch):
        monkeypatch.setattr(sim_market_data, "_TD_MIN_GAP_S", 0.0)
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        release = threading.Event()
        entered = threading.Event()

        class BlockingPool:
            def submit(self, fn, *args, **kwargs):
                entered.set()
                assert release.wait(timeout=15)
                fn(*args, **kwargs)
                return None

        monkeypatch.setattr(sim_market_data, "_POOL", BlockingPool())
        barrier = threading.Barrier(2)
        results: list = []

        def go():
            barrier.wait(timeout=15)
            results.append(sim_market_data.start_dataset_build(
                fake_db, assets=["EURUSD"], years=5))

        workers = [threading.Thread(target=go) for _ in range(2)]
        for t in workers:
            t.start()
        assert entered.wait(timeout=15)
        # The winner is parked inside submit holding the slot; give the
        # loser a chance to attempt, then let the winner run.
        for t in workers:
            t.join(timeout=5)
        release.set()
        for t in workers:
            t.join(timeout=30)
        assert len(results) == 2
        assert sum(1 for r in results if r.get("ok")) == 1
        assert sum(1 for r in results if not r.get("ok")) == 1
        assert sim_market_data.active_build() is None


class TestDatasetBuildStages:
    """Finding 2: `fetching_twelvedata` must precede the TD fetch call."""

    def test_twelvedata_stage_set_before_fetch(
            self, fake_db, inline_worker, monkeypatch):
        events: list = []
        orig_stage = sim_market_data._set_stage

        def spy_stage(db, version, stage):
            events.append(("stage", stage))
            return orig_stage(db, version, stage)

        async def spy_td(asset, *, years, client):
            events.append(("td_fetch", asset))
            return []

        monkeypatch.setattr(sim_market_data, "_set_stage", spy_stage)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            spy_td)
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        res = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5)
        assert res.get("ok") is True
        stages = [s for k, s in events if k == "stage"]
        assert "fetching_twelvedata" in stages
        idx_stage = events.index(("stage", "fetching_twelvedata"))
        idx_fetch = next(i for i, e in enumerate(events)
                         if e[0] == "td_fetch")
        assert idx_stage < idx_fetch
        assert (stages.index("fetching_biquote")
                < stages.index("fetching_twelvedata")
                < stages.index("validating"))
