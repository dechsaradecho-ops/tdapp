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

    def test_fallback_survives_capped_backend(self):
        """A backend capping every page at 1000 must not lose rows.

        Regression: with ``page_size=5000`` a ``len(page) < page_size``
        break (or ``offset += page_size``) stops/skips after the first
        capped page. Offset-by-received + empty-page-only break walks
        the whole version.
        """

        class _CappedFake(_NoPagedFake):
            def select(self, table, filters=None, order="created_at",
                       desc=True, limit=50, offset=0, columns="*"):
                rows = super().select(
                    table, filters=filters, order=order, desc=desc,
                    limit=1000000, offset=offset)
                return rows[:min(int(limit or 0), 1000)]

        db = _CappedFake()
        assert not hasattr(db, "select_paged")
        _seed_big_version(db, "v-capped", n=6500)
        rows = sim_market_data._select_all_bars_fallback(db, "v-capped")
        assert len(rows) == 6500
        ds = sim_market_data.load_dataset(db, "v-capped")
        assert len(ds["EURUSD"]) == 6500


# ---------------------------------------------------------------------------
# Task 1: base-version reuse + updatable datasets (RED first)
# ---------------------------------------------------------------------------
def _writes(db: FakeDatabase) -> tuple[int, int]:
    return (db.count(sim_market_data.META_TABLE) or 0,
            db.count(sim_market_data.BARS_TABLE) or 0)


def _seed_complete(db: FakeDatabase, version: str, assets: list[str],
                   n_bars: int = 300, source: str = "biquote",
                   base: float = 1.1000, status: str = "complete",
                   complete: bool = True,
                   started_at: str = "2026-11-27T00:00:00+00:00") -> None:
    db.insert(sim_market_data.META_TABLE, {
        "dataset_version": version,
        "status": status,
        "years": 5,
        "assets": list(assets),
        "started_at": started_at,
        "finished_at": (started_at if status in ("complete", "failed")
                        else None),
        "report": {"stage": status, "complete": complete,
                   "version": version},
        "data_hash": "seed",
        "code_version": sim_market_data.CODE_VERSION,
    })
    for asset in assets:
        for b in _drift_bars(asset, end_ago_days=1, n=n_bars,
                             base=base, source=source):
            db.insert(sim_market_data.BARS_TABLE, {
                "dataset_version": version,
                "asset": b.asset,
                "bar_date": b.bar_date,
                "open": b.open, "high": b.high, "low": b.low,
                "close": b.close,
                "source": b.source,
                "source_timestamp": b.source_timestamp,
            })


class TestBaseVersionRefusal:
    @pytest.mark.asyncio
    async def test_unknown_base_refused_zero_writes(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        before = _writes(fake_db)
        res = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5, base_version="v-nope")
        assert res["ok"] is False
        assert _writes(fake_db) == before

    @pytest.mark.asyncio
    async def test_incomplete_base_refused_zero_writes(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        _seed_complete(fake_db, "v-building", ["EURUSD"], status="building",
                       complete=False)
        before = _writes(fake_db)
        res = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5, base_version="v-building")
        assert res["ok"] is False
        assert _writes(fake_db) == before

    @pytest.mark.asyncio
    async def test_complete_status_but_report_incomplete_refused(
            self, fake_db):
        _seed_complete(fake_db, "v-mixed", ["EURUSD"], status="complete",
                       complete=False)
        before = _writes(fake_db)
        res = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5, base_version="v-mixed")
        assert res["ok"] is False
        assert _writes(fake_db) == before

    @pytest.mark.asyncio
    async def test_route_build_unknown_base_refused_zero_writes(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        before = _writes(fake_db)
        res = await call("POST", "/api/system/sim-data/build",
                         {"assets": ["EURUSD"], "years": 5,
                          "base_version": "v-nope"})
        assert res["ok"] is False
        assert _writes(fake_db) == before


class TestBaseReuse:
    @pytest.mark.asyncio
    async def test_reuse_skips_td_fetch_and_hash_stable(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        v1 = await call("POST", "/api/system/sim-data/build",
                        {"assets": ["EURUSD"], "years": 5})
        assert v1["ok"] is True and v1["status"] == "done"
        st1 = await call("GET", f"/api/system/sim-data/{v1['version']}")
        assert st1["status"] == "complete"

        td_calls: list = []
        bq_calls: list = []

        async def spy_td(asset, *, years, client):
            td_calls.append(asset)
            return await fake_td(asset, years=years, client=client)

        async def spy_bq(asset, client):
            bq_calls.append(asset)
            return await fake_bq(asset, client)

        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", spy_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            spy_td)
        v2 = await call("POST", "/api/system/sim-data/build",
                        {"assets": ["EURUSD"], "years": 5,
                         "base_version": v1["version"]})
        assert v2["ok"] is True, v2
        assert v2["status"] == "done", v2["report"]
        assert td_calls == []
        assert bq_calls == ["EURUSD"]
        st2 = await call("GET", f"/api/system/sim-data/{v2['version']}")
        rep = st2["report"]
        assert rep["base_version"] == v1["version"]
        assert rep["reused_bars"] > 0
        assert rep["reused_per_asset"]["EURUSD"] == rep["reused_bars"]
        assert rep["fetched_bars"] > 0
        assert st2["data_hash"] == st1["data_hash"]
        ds = sim_market_data.load_dataset(fake_db, v2["version"])
        dates = [c.t for c in ds["EURUSD"]]
        assert len(dates) == len(set(dates))
        assert len(dates) >= 250

    @pytest.mark.asyncio
    async def test_td_fetched_when_base_lacks_old_coverage(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        v1 = await call("POST", "/api/system/sim-data/build",
                        {"assets": ["EURUSD", "GBPUSD"], "years": 5})
        assert v1["ok"] is True
        # Wipe old-range (twelvedata) base bars for EURUSD only.
        for r in list(fake_db.rows.get(sim_market_data.BARS_TABLE, [])):
            if (r.get("dataset_version") == v1["version"]
                    and r.get("asset") == "EURUSD"
                    and r.get("source") == "twelvedata"):
                fake_db.rows[sim_market_data.BARS_TABLE].remove(r)

        td_calls: list = []

        async def spy_td(asset, *, years, client):
            td_calls.append(asset)
            return await fake_td(asset, years=years, client=client)

        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            spy_td)
        v2 = await call("POST", "/api/system/sim-data/build",
                        {"assets": ["EURUSD", "GBPUSD"], "years": 5,
                         "base_version": v1["version"]})
        assert v2["ok"] is True
        assert v2["status"] == "done", v2["report"]
        assert "EURUSD" in td_calls
        assert "GBPUSD" not in td_calls

    @pytest.mark.asyncio
    async def test_td_fetched_when_base_has_interior_hole(
            self, fake_db, inline_worker, monkeypatch):
        # One interior weekday TD bar removed: endpoints still covered, so
        # an endpoint-only check would wrongly skip the fetch and leave a
        # silent gap. Full weekday coverage must force the fetch.
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        v1 = await call("POST", "/api/system/sim-data/build",
                        {"assets": ["EURUSD"], "years": 5})
        assert v1["ok"] is True
        td_rows = [r for r in fake_db.rows[sim_market_data.BARS_TABLE]
                   if r.get("dataset_version") == v1["version"]
                   and r.get("source") == "twelvedata"]
        dates = sorted(r["bar_date"] for r in td_rows)
        hole = next(d for d in dates[1:-1]
                    if datetime.fromisoformat(d).weekday() < 5)
        fake_db.rows[sim_market_data.BARS_TABLE] = [
            r for r in fake_db.rows[sim_market_data.BARS_TABLE]
            if not (r.get("dataset_version") == v1["version"]
                    and r.get("bar_date") == hole
                    and r.get("source") == "twelvedata")]

        td_calls: list = []

        async def spy_td(asset, *, years, client):
            td_calls.append(asset)
            return await fake_td(asset, years=years, client=client)

        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            spy_td)
        v2 = await call("POST", "/api/system/sim-data/build",
                        {"assets": ["EURUSD"], "years": 5,
                         "base_version": v1["version"]})
        assert v2["ok"] is True
        assert v2["status"] == "done", v2["report"]
        assert td_calls == ["EURUSD"]

    @pytest.mark.asyncio
    async def test_fresh_biquote_overrides_base_same_date(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        v1 = await call("POST", "/api/system/sim-data/build",
                        {"assets": ["EURUSD"], "years": 5})
        assert v1["ok"] is True

        async def bq_shifted(asset, client):
            # One day older than fake_bq (overlaps base TD newest date)
            # with a ~1% level shift (well under the 20% spike gate).
            return _drift_bars(asset, end_ago_days=1, n=301,
                               base=1.1111, source="biquote")

        td_calls: list = []

        async def spy_td(asset, *, years, client):
            td_calls.append(asset)
            return await fake_td(asset, years=years, client=client)

        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent",
                            bq_shifted)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            spy_td)
        v2 = await call("POST", "/api/system/sim-data/build",
                        {"assets": ["EURUSD"], "years": 5,
                         "base_version": v1["version"]})
        assert v2["ok"] is True
        assert v2["status"] == "done", v2["report"]
        ds = sim_market_data.load_dataset(fake_db, v2["version"])
        by_t = {c.t: c for c in ds["EURUSD"]}
        import datetime as _dt
        overlap = _drift_bars("EURUSD", end_ago_days=1, n=301,
                              base=1.1111, source="biquote")[0]
        day = overlap.bar_date
        d = _dt.date.fromisoformat(day)
        t = int(_dt.datetime(d.year, d.month, d.day,
                             tzinfo=_dt.timezone.utc).timestamp())
        assert by_t[t].c == overlap.close
        rows = [r for r in fake_db.rows[sim_market_data.BARS_TABLE]
                if r.get("dataset_version") == v2["version"]
                and r.get("bar_date") == day]
        assert len(rows) == 1
        assert rows[0]["source"] == "biquote"
        seams = [s for s in v2["report"]["seam_discrepancies"]
                 if s["bar_date"] == day]
        assert seams and seams[0]["selected_source"] == "biquote"


class TestUpdateDataset:
    @pytest.mark.asyncio
    async def test_update_no_history_full_build(
            self, fake_db, inline_worker, monkeypatch):
        td_calls: list = []

        async def spy_td(asset, *, years, client):
            td_calls.append(asset)
            return await fake_td(asset, years=years, client=client)

        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            spy_td)
        res = sim_market_data.update_dataset(
            fake_db, assets=["EURUSD"], years=5)
        assert res["ok"] is True
        st = sim_market_data.dataset_build_status(fake_db, res["version"])
        assert st["status"] == "complete"
        assert td_calls == ["EURUSD"]
        assert st["report"]["base_version"] is None

    def test_update_no_history_no_assets_refused_zero_writes(
            self, fake_db):
        before = _writes(fake_db)
        res = sim_market_data.update_dataset(fake_db)
        assert res["ok"] is False
        assert _writes(fake_db) == before

    @pytest.mark.asyncio
    async def test_update_resolves_latest_complete(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        _seed_complete(fake_db, "v20261127-100000-aaaaaa", ["EURUSD"],
                       started_at="2026-11-26T00:00:00+00:00")
        _seed_complete(fake_db, "v20261127-110000-bbbbbb",
                       ["EURUSD", "GBPUSD"],
                       started_at="2026-11-27T00:00:00+00:00")
        # Realistic base: Twelve Data bars covering the old range so the
        # TD fetch is skipped and the base actually contributes (reused>0).
        # BQ seeds cover [300..1] days ago; these cover [370..301].
        for _asset in ["EURUSD", "GBPUSD"]:
            for _b in _drift_bars(_asset, end_ago_days=301, n=70,
                                  base=1.0500, source="twelvedata"):
                fake_db.insert(sim_market_data.BARS_TABLE, {
                    "dataset_version": "v20261127-110000-bbbbbb",
                    "asset": _b.asset,
                    "bar_date": _b.bar_date,
                    "open": _b.open, "high": _b.high,
                    "low": _b.low, "close": _b.close,
                    "source": _b.source,
                    "source_timestamp": _b.source_timestamp,
                })
        res = sim_market_data.update_dataset(fake_db, years=1)
        assert res["ok"] is True, res
        st = sim_market_data.dataset_build_status(fake_db, res["version"])
        assert st["status"] == "complete", st["report"]
        assert sorted(st["report"]["assets"]) == ["EURUSD", "GBPUSD"]
        assert st["report"]["base_version"] == "v20261127-110000-bbbbbb"

    @pytest.mark.asyncio
    async def test_update_unknown_base_refused_zero_writes(self, fake_db):
        before = _writes(fake_db)
        res = sim_market_data.update_dataset(
            fake_db, assets=["EURUSD"], base_version="v-nope")
        assert res["ok"] is False
        assert _writes(fake_db) == before

    def test_update_refused_while_build_active(self, fake_db):
        sim_market_data._set_active("live-build")
        before = _writes(fake_db)
        res = sim_market_data.update_dataset(fake_db, assets=["EURUSD"])
        assert res["ok"] is False
        assert res["version"] == "live-build"
        assert _writes(fake_db) == before

    @pytest.mark.asyncio
    async def test_route_update_mirrors_build(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        res = await call("POST", "/api/system/sim-data/update",
                         {"assets": ["EURUSD"], "years": 5})
        assert res["ok"] is True
        assert res["status"] == "done"
        assert res["db_status"] == "complete"
        assert res["report"]["complete"] is True

    @pytest.mark.asyncio
    async def test_route_update_validation(self, fake_db):
        res = await call("POST", "/api/system/sim-data/update",
                         {"assets": [], "years": 5})
        assert res["ok"] is False
        res = await call("POST", "/api/system/sim-data/update",
                         {"assets": ["EURUSD"], "years": "many"})
        assert res["ok"] is False

    @pytest.mark.asyncio
    async def test_route_update_refused_while_active(self, fake_db):
        sim_market_data._set_active("live-build")
        res = await call("POST", "/api/system/sim-data/update",
                         {"assets": ["EURUSD"]})
        assert res["ok"] is False

    @pytest.mark.asyncio
    async def test_update_disjoint_base_reports_no_base(
            self, fake_db, inline_worker, monkeypatch):
        """Explicit base with a disjoint asset set contributes zero bars.

        The report must record ``base_version=None`` (with
        ``reused_bars == 0``) — a pointer to a version that contributed
        nothing is misleading.
        """
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        _seed_complete(fake_db, "v-base-eurusd", ["EURUSD"])
        res = sim_market_data.update_dataset(
            fake_db, assets=["GBPUSD"], years=5,
            base_version="v-base-eurusd")
        assert res["ok"] is True, res
        st = sim_market_data.dataset_build_status(fake_db, res["version"])
        assert st["status"] == "complete", st["report"]
        assert st["report"]["reused_bars"] == 0
        assert st["report"]["base_version"] is None


class TestPersistTuning:
    @pytest.mark.asyncio
    async def test_identical_rows_across_batch_sizes(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)

        def dump(version):
            rows = [r for r in fake_db.rows[sim_market_data.BARS_TABLE]
                    if r.get("dataset_version") == version]
            return sorted(
                (r["asset"], r["bar_date"], r["open"], r["high"],
                 r["low"], r["close"], r["source"],
                 r.get("source_timestamp"))
                for r in rows)

        monkeypatch.setattr(sim_market_data, "_PERSIST_BATCH_SIZE", 500)
        r1 = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5)
        assert r1.get("ok") is True
        monkeypatch.setattr(sim_market_data, "_PERSIST_BATCH_SIZE", 5000)
        r2 = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5)
        assert r2.get("ok") is True
        assert dump(r1["version"]) == dump(r2["version"])
        h1 = sim_market_data.dataset_build_status(
            fake_db, r1["version"])["data_hash"]
        h2 = sim_market_data.dataset_build_status(
            fake_db, r2["version"])["data_hash"]
        assert h1 == h2


class TestLatestComplete:
    def _seed(self, fake_db, version, status="complete",
              complete=True):
        fake_db.insert(sim_market_data.META_TABLE, {
            "dataset_version": version,
            "status": status,
            "years": 5,
            "assets": ["EURUSD"],
            "started_at": "2026-11-27T00:00:00+00:00",
            "finished_at": "2026-11-27T00:00:00+00:00",
            "report": {"stage": status, "complete": complete,
                       "version": version},
            "data_hash": "seed",
            "code_version": sim_market_data.CODE_VERSION,
        })

    @pytest.mark.asyncio
    async def test_latest_of_several_complete_wins(self, fake_db):
        self._seed(fake_db, "v20261127-100000-aaaaaa")
        self._seed(fake_db, "v20261127-110000-bbbbbb")
        res = await call("GET", "/api/system/sim-data/latest")
        assert res["ok"] is True
        assert res["version"] == "v20261127-110000-bbbbbb"
        assert res["status"] == "complete"

    @pytest.mark.asyncio
    async def test_newer_building_and_failed_ignored(self, fake_db):
        self._seed(fake_db, "v20261127-100000-aaaaaa")
        self._seed(fake_db, "v20261127-120000-cccccc",
                   status="building", complete=False)
        self._seed(fake_db, "v20261127-130000-dddddd",
                   status="failed", complete=False)
        self._seed(fake_db, "v20261127-140000-eeeeee",
                   status="complete", complete=False)
        res = await call("GET", "/api/system/sim-data/latest")
        assert res["ok"] is True
        assert res["version"] == "v20261127-100000-aaaaaa"

    @pytest.mark.asyncio
    async def test_empty_db_refuses_cleanly(self, fake_db):
        before = _writes(fake_db)
        res = await call("GET", "/api/system/sim-data/latest")
        assert res["ok"] is False
        assert res["error"] == "no complete dataset"
        assert _writes(fake_db) == before


# ---------------------------------------------------------------------------
# Directed fix: bulk persist (one array-POST per batch, not per bar)
# ---------------------------------------------------------------------------
def _version_dump(db: FakeDatabase, version: str) -> list[tuple]:
    rows = [r for r in db.rows.get(sim_market_data.BARS_TABLE, [])
            if r.get("dataset_version") == version]
    return sorted(
        (r["asset"], r["bar_date"], r["open"], r["high"],
         r["low"], r["close"], r["source"], r.get("source_timestamp"))
        for r in rows)


def _counting_db(db: FakeDatabase, monkeypatch,
                 counter: dict) -> FakeDatabase:
    orig = db.insert

    def spy(table: str, row):
        if table == sim_market_data.BARS_TABLE:
            counter["calls"] += 1
            counter["rows"] += (len(row) if isinstance(row, list) else 1)
        return orig(table, row)

    monkeypatch.setattr(db, "insert", spy)
    return db


class TestBulkPersist:
    """Bulk (array-POST per batch) vs legacy (per-row) persist paths."""

    @pytest.mark.asyncio
    async def test_bulk_matches_legacy_rows_and_hash(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        monkeypatch.setattr(sim_market_data, "_BULK_PERSIST", False)
        legacy = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5)
        assert legacy.get("ok") is True
        monkeypatch.setattr(sim_market_data, "_BULK_PERSIST", True)
        bulk = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5)
        assert bulk.get("ok") is True
        assert (_version_dump(fake_db, bulk["version"])
                == _version_dump(fake_db, legacy["version"]))
        assert (sim_market_data.dataset_build_status(
                    fake_db, bulk["version"])["data_hash"]
                == sim_market_data.dataset_build_status(
                    fake_db, legacy["version"])["data_hash"])
        st = sim_market_data.dataset_build_status(
            fake_db, bulk["version"])
        assert st["status"] == "complete"

    @pytest.mark.asyncio
    async def test_bulk_cuts_insert_calls(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        import math as _math
        n_bars = len(_version_dump(fake_db, "v-none"))  # 0; sizes below
        assert n_bars == 0
        # Legacy: exactly one insert call per bar.
        counter: dict = {"calls": 0, "rows": 0}
        _counting_db(fake_db, monkeypatch, counter)
        monkeypatch.setattr(sim_market_data, "_BULK_PERSIST", False)
        legacy = sim_market_data.start_dataset_build(
            fake_db, assets=["EURUSD"], years=5)
        assert legacy.get("ok") is True
        bars = len(_version_dump(fake_db, legacy["version"]))
        assert bars > 500  # multi-batch version
        assert counter["calls"] == bars == counter["rows"]
        # Bulk: one call per 500-row batch, same rows.
        db2 = _counting_db(FakeDatabase(), monkeypatch,
                           counter := {"calls": 0, "rows": 0})
        set_state(db2)
        monkeypatch.setattr(sim_market_data, "_BULK_PERSIST", True)
        bulk = sim_market_data.start_dataset_build(
            db2, assets=["EURUSD"], years=5)
        assert bulk.get("ok") is True
        assert counter["rows"] == bars
        assert counter["calls"] == _math.ceil(bars / 500)
        assert counter["calls"] < bars
        assert (_version_dump(db2, bulk["version"])
                == _version_dump(fake_db, legacy["version"]))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bulk", [True, False])
    async def test_failed_batch_fails_version_loudly(
            self, inline_worker, monkeypatch, bulk):
        from tests.test_workers import FakeDatabase as _Fake
        db = _Fake(fail_tables={sim_market_data.BARS_TABLE})
        set_state(db)
        monkeypatch.setattr(sim_market_data, "fetch_biquote_recent", fake_bq)
        monkeypatch.setattr(sim_market_data, "fetch_twelvedata_history",
                            fake_td)
        monkeypatch.setattr(sim_market_data, "_BULK_PERSIST", bulk)
        res = sim_market_data.start_dataset_build(
            db, assets=["EURUSD"], years=5)
        assert res.get("ok") is True  # build starts; persist then fails
        st = sim_market_data.dataset_build_status(db, res["version"])
        assert st["status"] == "failed"
        assert st["report"]["complete"] is False


# ---------------------------------------------------------------------------
# Task 4: defensive date-unique load (prod incident 2026-11-27 — duplicate
# AUDNZD 2022-03-09 bar on a `complete` version failed replay at
# build_timeline). Read path (select_paged multi-page 20k+ reads) must be
# robust to dupe rows from any source: first-in-order wins, loud warning,
# never raise, never partial {}.
# ---------------------------------------------------------------------------
def _seed_complete_with_rows(fake_db, version, rows):
    fake_db.insert(sim_market_data.META_TABLE, {
        "dataset_version": version,
        "status": "complete",
        "years": 5,
        "assets": ["AUDNZD"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "report": {"stage": "done", "complete": True, "version": version},
        "data_hash": "seed",
        "code_version": sim_market_data.CODE_VERSION,
    })
    fake_db.rows.setdefault(sim_market_data.BARS_TABLE, []).extend(rows)


def _bar(version, date, o=1.0, h=1.1, l=0.9, c=1.05, source="biquote"):
    return {
        "dataset_version": version,
        "asset": "AUDNZD",
        "bar_date": date,
        "open": o, "high": h, "low": l, "close": c,
        "source": source,
        "source_timestamp": None,
    }


class TestLoadDatasetDedupe:
    def test_identical_duplicate_rows_single_candle(self, fake_db, caplog):
        import logging as _logging
        dup = _bar("v-dupe", "2022-03-09")
        _seed_complete_with_rows(fake_db, "v-dupe", [
            _bar("v-dupe", "2022-03-08"),
            dict(dup),
            dict(dup),
            _bar("v-dupe", "2022-03-10"),
        ])
        with caplog.at_level(_logging.WARNING,
                             logger="app.services.sim_market_data"):
            ds = sim_market_data.load_dataset(fake_db, "v-dupe")
        assert set(ds) == {"AUDNZD"}
        assert len(ds["AUDNZD"]) == 3
        assert any("v-dupe" in (r.getMessage() or "")
                   and "2022-03-09" in (r.getMessage() or "")
                   and "AUDNZD" in (r.getMessage() or "")
                   and "byte-identical read artifact" in (r.getMessage() or "")
                   for r in caplog.records)

    def test_conflicting_duplicate_first_wins_and_warns(
            self, fake_db, caplog):
        import logging as _logging
        _seed_complete_with_rows(fake_db, "v-conflict", [
            _bar("v-conflict", "2022-03-09", c=1.05),
            _bar("v-conflict", "2022-03-09", c=1.99,
                 o=1.9, source="twelvedata"),
        ])
        with caplog.at_level(_logging.WARNING,
                             logger="app.services.sim_market_data"):
            ds = sim_market_data.load_dataset(fake_db, "v-conflict")
        assert len(ds["AUDNZD"]) == 1
        assert ds["AUDNZD"][0].c == 1.05
        assert any("v-conflict" in (r.getMessage() or "")
                   and "2022-03-09" in (r.getMessage() or "")
                   and "AUDNZD" in (r.getMessage() or "")
                   and "conflicting values" in (r.getMessage() or "")
                   for r in caplog.records)

    def test_multi_page_shaped_sorted_input_order_preserved(
            self, fake_db):
        rows = ([_bar("v-pages", f"2022-03-{d:02d}")
                 for d in range(1, 9)]
                + [_bar("v-pages", "2022-03-09")]
                + [_bar("v-pages", "2022-03-09")]
                + [_bar("v-pages", f"2022-03-{d:02d}")
                   for d in range(10, 15)])
        _seed_complete_with_rows(fake_db, "v-pages", rows)
        ds = sim_market_data.load_dataset(fake_db, "v-pages")
        candles = ds["AUDNZD"]
        assert len(candles) == 14
        assert [c.t for c in candles] == sorted(c.t for c in candles)


# ---------------------------------------------------------------------------
# Task 5: deterministic per-asset dataset reads (prod incident 2026-11-27,
# part 2 — identical configs replayed different bars because the global
# date-ordered offset-paged read walks ~28 assets sharing each date: offset
# pages over those heavy sort-key ties can return a row twice or skip it).
# load_dataset must read per asset (bar_date is UNIQUE within one asset by
# PK), never via a global date-ordered multi-asset page walk.
# ---------------------------------------------------------------------------
class _TieSkewFake(FakeDatabase):
    """Models the incident: the GLOBAL date-ordered page walk is
    tie-unstable (dupes one boundary row, skips another), while a
    PER-ASSET read (unique bar_date order) is exact.

    select_paged calls are recorded so tests can prove load_dataset never
    issues a global (asset-less) bars read.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.paged_calls: list[dict] = []

    def select_paged(self, table, filters=None, order="created_at",
                     desc=False, page_size=1000, max_rows=20000):
        self.paged_calls.append(dict(filters or {}))
        rows = [r for r in self.rows.get(table, [])
                if all(r.get(c) == v for c, v in (filters or {}).items())]
        if (table == sim_market_data.BARS_TABLE
                and not (filters or {}).get("asset")
                and len(rows) >= 2):
            rows = [rows[0]] + rows  # dupe the first row ...
            del rows[2]  # ... and skip the real second row
        return rows


def _seed_tie_version(db, version, assets, dates):
    db.insert(sim_market_data.META_TABLE, {
        "dataset_version": version,
        "status": "complete",
        "years": 5,
        "assets": list(assets),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "report": {"stage": "done", "complete": True, "version": version},
        "data_hash": "tie",
        "code_version": sim_market_data.CODE_VERSION,
    })
    for asset in assets:
        for i, day in enumerate(dates):
            db.insert(sim_market_data.BARS_TABLE, {
                "dataset_version": version,
                "asset": asset,
                "bar_date": day,
                "open": 1.0 + i * 0.001,
                "high": 1.1 + i * 0.001,
                "low": 0.9 + i * 0.001,
                "close": 1.05 + i * 0.001,
                "source": "biquote",
                "source_timestamp": None,
            })


class TestLoadDatasetPerAssetDeterministic:
    _ASSETS = ["EURUSD", "GBPUSD", "AUDNZD"]
    _DATES = [f"2022-03-{d:02d}" for d in range(1, 11)]

    def test_global_tie_skew_cannot_corrupt_load(self, caplog):
        """The old failure mode, simulated: the fake's GLOBAL page walk
        dupes/skips rows, yet load_dataset returns exactly one Candle per
        (asset, date) in sorted order with zero dedupe warnings."""
        import logging as _logging
        db = _TieSkewFake()
        _seed_tie_version(db, "v-tie", self._ASSETS, self._DATES)
        # Sanity: the fake really models the incident on the global path
        # (same row count, but one pair duped and another pair missing).
        skewed = db.select_paged(
            sim_market_data.BARS_TABLE,
            filters={"dataset_version": "v-tie"}, order="bar_date")
        assert len({(r["asset"], r["bar_date"]) for r in skewed}) == (
            len(self._ASSETS) * len(self._DATES) - 1)
        db.paged_calls.clear()
        with caplog.at_level(_logging.WARNING,
                             logger="app.services.sim_market_data"):
            ds = sim_market_data.load_dataset(db, "v-tie")
        assert set(ds) == set(self._ASSETS)
        for asset in self._ASSETS:
            assert len(ds[asset]) == len(self._DATES)
            assert [c.t for c in ds[asset]] == sorted(c.t for c in ds[asset])
        assert not [r for r in caplog.records
                    if r.levelno >= _logging.WARNING
                    and r.name == "app.services.sim_market_data"], \
            "clean per-asset reads must leave the Task 4 dedupe a no-op"
        # No global bars read: every round-trip is scoped to one asset.
        assert db.paged_calls, "expected per-asset DB round-trips"
        assert all("asset" in call for call in db.paged_calls)
        assert len(db.paged_calls) <= len(self._ASSETS) + 1

    def test_explicit_assets_subset_reads_only_requested(self):
        db = _TieSkewFake()
        _seed_tie_version(db, "v-tie-sub", self._ASSETS, self._DATES)
        ds = sim_market_data.load_dataset(db, "v-tie-sub", ["GBPUSD"])
        assert set(ds) == {"GBPUSD"}
        assert len(ds["GBPUSD"]) == len(self._DATES)
        assert db.paged_calls == [
            {"dataset_version": "v-tie-sub", "asset": "GBPUSD"}]

    def test_discovery_without_meta_assets_still_exact(self):
        """Meta rows predating the assets column: the set is learned from
        the bars table (distinct-asset read, whole-version read only to
        learn the set) — bars still come from per-asset reads."""
        db = _TieSkewFake()
        _seed_tie_version(db, "v-tie-nometa", self._ASSETS, self._DATES)
        for row in db.rows[sim_market_data.META_TABLE]:
            if row.get("dataset_version") == "v-tie-nometa":
                row.pop("assets", None)
        ds = sim_market_data.load_dataset(db, "v-tie-nometa")
        assert set(ds) == set(self._ASSETS)
        for asset in self._ASSETS:
            assert len(ds[asset]) == len(self._DATES)
        assert all("asset" in call for call in db.paged_calls)

    def test_base_snapshot_survives_tie_skew(self):
        """Review round 1: `_resolve_base` snapshots base bars through the
        same per-asset deterministic read, so a tie-unstable global page
        walk cannot bake skewed bars into a new immutable version."""
        db = _TieSkewFake()
        _seed_tie_version(db, "v-tie-base", self._ASSETS, self._DATES)
        bv, cleaned, meta_assets = sim_market_data._resolve_base(
            db, "v-tie-base", "2022-01-01", "2026-01-01")
        assert bv == "v-tie-base"
        assert meta_assets == sorted(self._ASSETS)
        assert set(cleaned) == set(self._ASSETS)
        for asset in self._ASSETS:
            assert [b.bar_date for b in cleaned[asset]] == self._DATES
        assert db.paged_calls, "expected per-asset DB round-trips"
        assert all("asset" in call for call in db.paged_calls)

    def test_base_snapshot_without_meta_assets_still_exact(self):
        """Base meta rows predating the assets column: the set is learned
        from the bars table — the snapshot itself still comes from
        per-asset reads, never the skewed global walk."""
        db = _TieSkewFake()
        _seed_tie_version(db, "v-tie-base-nometa", self._ASSETS, self._DATES)
        for row in db.rows[sim_market_data.META_TABLE]:
            if row.get("dataset_version") == "v-tie-base-nometa":
                row.pop("assets", None)
        bv, cleaned, meta_assets = sim_market_data._resolve_base(
            db, "v-tie-base-nometa", "2022-01-01", "2026-01-01")
        assert bv == "v-tie-base-nometa"
        assert meta_assets == []
        assert set(cleaned) == set(self._ASSETS)
        for asset in self._ASSETS:
            assert [b.bar_date for b in cleaned[asset]] == self._DATES
        assert all("asset" in call for call in db.paged_calls)
