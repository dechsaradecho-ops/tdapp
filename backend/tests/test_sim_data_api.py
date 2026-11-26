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
    # Years-aware: reach the requested cutoff so the years>2 TD/earliest
    # gate passes for the happy-path builds (real 5y history is ~1825
    # calendar days; BQ covers the most recent 300).
    n = max(300, int(years or 5) * 365)
    return _drift_bars(asset, end_ago_days=301, n=n,
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


# ---------------------------------------------------------------------------
# Final fix wave: coverage gate (Finding 1)
# ---------------------------------------------------------------------------
class TestCoverageGateYears:
    async def test_td_empty_years5_fails(
            self, fake_db, inline_worker, monkeypatch):
        async def bq_499(asset, client):
            return _drift_bars(asset, end_ago_days=1, n=499,
                               base=1.1000, source="biquote")

        async def td_empty(asset, *, years, client):
            return []

        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", bq_499)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            td_empty)
        res = await call("POST", "/api/system/sim-data/build",
                         {"assets": ["EURUSD"], "years": 5})
        assert res["ok"] is True
        assert res["status"] == "failed"
        version = res["version"]
        st = await call("GET", f"/api/system/sim-data/{version}")
        assert st["status"] == "failed"
        assert st["report"]["complete"] is False
        assert "EURUSD" in st["report"]["failed_assets"]

    async def test_biquote_only_years1_passes(
            self, fake_db, inline_worker, monkeypatch):
        async def bq_499(asset, client):
            return _drift_bars(asset, end_ago_days=1, n=499,
                               base=1.1000, source="biquote")

        async def td_empty(asset, *, years, client):
            return []

        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", bq_499)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            td_empty)
        res = await call("POST", "/api/system/sim-data/build",
                         {"assets": ["EURUSD"], "years": 1})
        assert res["ok"] is True
        assert res["status"] == "done"
        st = await call("GET", f"/api/system/sim-data/{res['version']}")
        assert st["status"] == "complete"
        assert st["report"]["complete"] is True


# ---------------------------------------------------------------------------
# Final fix wave: load_dataset gating + DB errors (Finding 2)
# ---------------------------------------------------------------------------
class TestLoadDatasetGating:
    def _seed_version(self, fake_db, version, status, complete,
                      n_bars=5):
        fake_db.insert(sim_market_data.META_TABLE, {
            "dataset_version": version,
            "status": status,
            "years": 5,
            "assets": ["EURUSD"],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": (datetime.now(timezone.utc).isoformat()
                            if status in ("complete", "failed") else None),
            "report": {"stage": status, "complete": complete,
                       "version": version},
            "data_hash": "abc",
            "code_version": sim_market_data.CODE_VERSION,
        })
        for i in range(n_bars):
            day = (datetime.now(timezone.utc).date()
                   - timedelta(days=10 + i)).isoformat()
            fake_db.insert(sim_market_data.BARS_TABLE, {
                "dataset_version": version,
                "asset": "EURUSD",
                "bar_date": day,
                "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
                "source": "biquote",
                "source_timestamp": None,
            })

    def test_complete_loads(self, fake_db):
        self._seed_version(fake_db, "v-complete", "complete", True)
        ds = sim_market_data.load_dataset(fake_db, "v-complete")
        assert set(ds) == {"EURUSD"}
        assert len(ds["EURUSD"]) == 5

    def test_building_returns_empty(self, fake_db):
        self._seed_version(fake_db, "v-building", "building", False)
        assert sim_market_data.load_dataset(fake_db, "v-building") == {}

    def test_failed_returns_empty(self, fake_db):
        self._seed_version(fake_db, "v-failed", "failed", False)
        assert sim_market_data.load_dataset(fake_db, "v-failed") == {}

    def test_unknown_returns_empty(self, fake_db):
        assert sim_market_data.load_dataset(fake_db, "v-nope") == {}

    def test_status_complete_but_report_incomplete_returns_empty(
            self, fake_db):
        self._seed_version(fake_db, "v-mixed", "complete", False)
        assert sim_market_data.load_dataset(fake_db, "v-mixed") == {}

    def test_meta_db_error_raises(self, fake_db, monkeypatch):
        self._seed_version(fake_db, "v-complete", "complete", True)

        def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(fake_db, "select_ex", boom)
        monkeypatch.setattr(fake_db, "select", boom)
        import pytest as _pt
        with _pt.raises(sim_market_data.SimMarketDataError):
            sim_market_data.load_dataset(fake_db, "v-complete")

    def test_bars_db_error_raises(self, fake_db, monkeypatch):
        self._seed_version(fake_db, "v-complete2", "complete", True)

        orig_paged = fake_db.select_paged

        def boom_paged(*a, **k):
            raise RuntimeError("bars down")

        monkeypatch.setattr(fake_db, "select_paged", boom_paged)
        import pytest as _pt
        with _pt.raises(sim_market_data.SimMarketDataError):
            sim_market_data.load_dataset(fake_db, "v-complete2")
        monkeypatch.setattr(fake_db, "select_paged", orig_paged)


# ---------------------------------------------------------------------------
# Final fix wave: _meta_patch safety (Finding 3)
# ---------------------------------------------------------------------------
class TestMetaPatchSafety:
    def _seed_building(self, fake_db, version="v-meta"):
        fake_db.insert(sim_market_data.META_TABLE, {
            "dataset_version": version,
            "status": "building",
            "years": 5,
            "assets": ["EURUSD"],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "report": {"stage": "persisting", "version": version},
            "data_hash": None,
            "code_version": sim_market_data.CODE_VERSION,
        })
        return version

    def test_update_in_place_no_delete(self, fake_db):
        v = self._seed_building(fake_db)
        before = fake_db.count(sim_market_data.META_TABLE,
                               {"dataset_version": v})
        ok = sim_market_data._meta_patch(fake_db, v, {"status": "failed"})
        assert ok is True
        after = fake_db.count(sim_market_data.META_TABLE,
                              {"dataset_version": v})
        assert after == before == 1
        rows = fake_db.select(sim_market_data.META_TABLE,
                              filters={"dataset_version": v}, limit=1)
        assert rows[0]["status"] == "failed"

    def test_failed_write_preserves_row(self, fake_db, monkeypatch):
        v = self._seed_building(fake_db)
        monkeypatch.setattr(fake_db, "update", lambda *a, **k: False)
        monkeypatch.setattr(fake_db, "insert", lambda *a, **k: None)
        ok = sim_market_data._meta_patch(fake_db, v, {"status": "failed"})
        assert ok is False
        rows = [r for r in fake_db.rows.get(sim_market_data.META_TABLE, [])
                if r.get("dataset_version") == v]
        assert len(rows) == 1
        assert rows[0]["status"] == "building"

    def test_run_build_final_write_failure_ends_failed(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        real_patch = sim_market_data._meta_patch
        calls = {"n": 0}

        def flaky_patch(db, version, changes):
            # Fail the first terminal verdict (has finished_at), then heal.
            if changes.get("finished_at") and calls["n"] == 0:
                calls["n"] += 1
                return False
            return real_patch(db, version, changes)

        monkeypatch.setattr(sim_market_data, "_meta_patch", flaky_patch)
        res = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5)
        assert res.get("ok") is True
        st = sim_market_data.dataset_build_status(fake_db, res["version"])
        assert st["status"] == "failed"
        assert st["report"]["complete"] is False


# ---------------------------------------------------------------------------
# Final fix wave: paged fallback beyond 20k (Finding 5)
# ---------------------------------------------------------------------------
class _NoPagedFake:
    """Minimal fake WITHOUT select_paged to force the offset/limit fallback."""

    def __init__(self):
        self.rows: dict[str, list[dict]] = {}
        self.available = True

    def insert(self, table, row):
        self.rows.setdefault(table, []).insert(
            0, {**row, "id": f"fake-{len(self.rows.get(table, []))}"})
        return row

    def select(self, table, filters=None, order="created_at", desc=True,
               limit=50, offset=0, columns="*"):
        rows = list(self.rows.get(table, []))
        for col, val in (filters or {}).items():
            rows = [r for r in rows if r.get(col) == val]
        rev = desc
        if order in rows[0] if rows else False:
            pass
        # bar_date ordering like the real query path
        try:
            rows.sort(key=lambda r: str(r.get(order) or ""), reverse=rev)
        except Exception:
            pass
        return rows[offset:offset + limit]

    def select_ex(self, table, filters=None, order="created_at", desc=True,
                  limit=50, offset=0):
        return self.select(table, filters=filters, order=order, desc=desc,
                            limit=limit, offset=offset)

    def update(self, table, row_id, changes):
        for r in self.rows.get(table, []):
            if r.get("id") == row_id:
                r.update(changes)
                return True
        return False

    def delete(self, table, filters):
        rows = self.rows.get(table, [])
        kept = [r for r in rows
                if not all(r.get(c) == v for c, v in filters.items())]
        self.rows[table] = kept
        return len(kept) < len(rows)

    def count(self, table, filters=None, **kw):
        rows = list(self.rows.get(table, []))
        for col, val in (filters or {}).items():
            rows = [r for r in rows if r.get(col) == val]
        return len(rows)


def _seed_big_version(db, version, n=20500):
    db.insert(sim_market_data.META_TABLE, {
        "dataset_version": version,
        "status": "complete",
        "years": 5,
        "assets": ["EURUSD"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "report": {"stage": "done", "complete": True, "version": version},
        "data_hash": "big",
        "code_version": sim_market_data.CODE_VERSION,
    })
    base = datetime(2000, 1, 1, tzinfo=timezone.utc).date()
    bars = []
    for i in range(n):
        day = (base + timedelta(days=i)).isoformat()
        bars.append({
            "dataset_version": version,
            "asset": "EURUSD",
            "bar_date": day,
            "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
            "source": "biquote" if i % 2 == 0 else "twelvedata",
            "source_timestamp": None,
        })
    db.rows.setdefault(sim_market_data.BARS_TABLE, []).extend(bars)


class TestPagedFallback:
    def test_load_dataset_fallback_returns_all_rows(self):
        db = _NoPagedFake()
        assert not hasattr(db, "select_paged")
        _seed_big_version(db, "v-big", n=20500)
        ds = sim_market_data.load_dataset(db, "v-big")
        assert len(ds["EURUSD"]) == 20500

    async def test_status_endpoint_fallback_counts_all_rows(self):
        db = _NoPagedFake()
        _seed_big_version(db, "v-big2", n=20500)
        set_state(db)  # type: ignore[arg-type]
        try:
            res = await call("GET", "/api/system/sim-data/v-big2")
        finally:
            set_state(FakeDatabase())
        assert res["ok"] is True
        assert res["bars"] == 20500
        assert res["per_asset_counts"]["EURUSD"] == 20500
