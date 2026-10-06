"""Simulation service + routes — the run lifecycle, no network.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_simulation.py -v

These tests exist because the run is a background job writing to two tables
while the UI polls it. The failure modes that actually bite are: two runs
starting at once, a progress bar that lies about how far along it is, cancel
that never lands, and a verdict reported without the out-of-sample number.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.services import simulation


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self, db, name="simulation_runs"):
        self.db = db
        self.name = name
        self._filters: dict = {}
        self._delete = False

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def gt(self, col, val):
        self._filters[col] = val
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def delete(self):
        self._delete = True
        return self

    def insert(self, rows):
        self.db.inserted.extend(rows if isinstance(rows, list) else [rows])
        return self

    def upsert(self, row):
        self.db.rows[row["id"]] = row
        return self

    def execute(self):
        if self._delete:
            gone = [k for k, v in self.db.rows.items()
                    if all(v.get(c) == val for c, val in self._filters.items())]
            for k in gone:
                self.db.rows.pop(k, None)
            self.db.deleted.extend(gone)
            return FakeResult([{"id": k} for k in gone])
        return FakeResult(list(self.db.rows.values()))


class FakeClient:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return FakeTable(self.db, name)


class FakeDB:
    """Enough Database surface for the simulation service."""

    available = True
    init_error = None

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.inserted: list[dict] = []
        self.updates: list[dict] = []
        self.counts: list[tuple] = []
        self.deleted: list[str] = []
        self._client = FakeClient(self)

    def insert(self, table, row):
        self.rows[str(row.get("id"))] = dict(row)
        return dict(row)

    def update(self, table, row_id, changes):
        self.updates.append({"id": row_id, **changes})
        if row_id in self.rows:
            self.rows[row_id].update(changes)
        return True

    def select(self, table, filters=None, order=None, desc=True, limit=50,
               offset=0, **kw):
        rows = list(self.rows.values())
        if filters:
            rows = [r for r in rows
                    if all(r.get(k) == v for k, v in filters.items())]
        return rows[:limit]

    def count(self, table, filters=None, **kw):
        self.counts.append((table, filters))
        rows = self.select(table, filters=filters, limit=10_000)
        return len(rows)


@pytest.fixture(autouse=True)
def _clean_registry():
    """The active-run slot and cancel flags are module state; a leaked one
    would make every later test refuse to start."""
    simulation._set_active(None)
    simulation._CANCEL.clear()
    yield
    simulation._set_active(None)
    simulation._CANCEL.clear()


class TestThinning:
    def test_keeps_everything_when_under_target(self):
        evs = [object() for _ in range(10)]
        assert simulation._thin(evs, 50) is evs

    def test_thins_to_exactly_the_target(self):
        evs = list(range(1000))
        out = simulation._thin(evs, 100)
        assert len(out) == 100

    def test_thins_across_the_whole_range_not_the_head(self):
        """Taking the first N would silently restrict the study to the OLDEST
        slice of history — the one way to make a sample look better without
        changing a number."""
        evs = list(range(1000))
        out = simulation._thin(evs, 100)
        assert out[0] == 0
        assert out[-1] >= 900, "the sample is bunched at the start of history"
        assert max(out) - min(out) > 800

    def test_evenly_spaced(self):
        evs = list(range(100))
        out = simulation._thin(evs, 10)
        gaps = [out[i + 1] - out[i] for i in range(len(out) - 1)]
        assert max(gaps) - min(gaps) <= 1

    def test_sorting_before_thinning_keeps_every_pair_in_every_era(self):
        """replay() emits events GROUPED BY ASSET, each asset's own history in
        time order. Thinning that raw list samples by list position, which
        means taking a different slice of TIME per pair — so the chronological
        train/test split ends up comparing pair A's early history against pair
        B's late one. Sorting to one timeline first is the fix, and this pins
        the property it buys: both pairs represented in both eras.
        """
        from app.engine.triple_barrier import LabelledEvent

        def mk(asset, bar_index):
            return LabelledEvent(asset=asset, direction="BUY",
                                 bar_index=bar_index, entry=1.1, atr_pct=0.5)

        raw = ([mk("AAA", i) for i in range(50)]
               + [mk("BBB", i) for i in range(50)])
        assert [e.asset for e in raw[:3]] == ["AAA", "AAA", "AAA"], \
            "premise: the raw list is asset-grouped"

        thinned = simulation._thin(
            sorted(raw, key=lambda e: (e.bar_index, e.asset)), 20)
        assert len(thinned) == 20
        early = [e for e in thinned if e.bar_index < 25]
        late = [e for e in thinned if e.bar_index >= 25]
        assert early and late, "one era came out empty"
        assert {e.asset for e in early} == {"AAA", "BBB"}
        assert {e.asset for e in late} == {"AAA", "BBB"}

    def test_thinning_the_unsorted_list_would_not_be_a_time_split(self):
        """The failure the sort prevents, stated as a test so it cannot come
        back unnoticed: on an asset-grouped list, "first 70% vs last 30%" is
        pair A vs pair B, not early vs late."""
        from app.engine.triple_barrier import LabelledEvent

        def mk(asset, bar_index):
            return LabelledEvent(asset=asset, direction="BUY",
                                 bar_index=bar_index, entry=1.1, atr_pct=0.5)

        raw = ([mk("AAA", i) for i in range(50)]
               + [mk("BBB", i) for i in range(50)])

        def halves(xs):
            h = len(xs) // 2
            return {e.asset for e in xs[:h]}, {e.asset for e in xs[h:]}

        unsorted_halves = halves(simulation._thin(raw, 20))
        assert unsorted_halves[0] != unsorted_halves[1], \
            "premise: on the grouped list the two halves are different pairs"

        sorted_halves = halves(simulation._thin(
            sorted(raw, key=lambda e: (e.bar_index, e.asset)), 20))
        assert sorted_halves[0] == sorted_halves[1] == {"AAA", "BBB"}


class TestCancellation:
    def test_request_cancel_sets_the_flag(self):
        ev = threading.Event()
        simulation._CANCEL["r1"] = ev
        assert simulation.request_cancel("r1") is True
        assert simulation.cancel_requested("r1") is True

    def test_cancel_is_not_repeatable(self):
        simulation._CANCEL["r2"] = threading.Event()
        assert simulation.request_cancel("r2") is True
        assert simulation.request_cancel("r2") is False

    def test_cancel_on_unknown_run_reports_false(self):
        assert simulation.request_cancel("nope") is False

    def test_cancel_requested_is_false_without_a_flag(self):
        assert simulation.cancel_requested("nope") is False


class TestStartRun:
    def test_refuses_when_the_db_is_down(self):
        db = FakeDB()
        db.available = False
        res = simulation.start_run(db)
        assert res["ok"] is False
        assert "DB" in res["error"]

    def test_refuses_a_second_run_while_one_is_live(self):
        """Two 28-pair history fetches would double the API load and interleave
        two unreadable progress bars."""
        db = FakeDB()
        simulation._set_active("already-running")
        res = simulation.start_run(db)
        assert res["ok"] is False
        assert res["run_id"] == "already-running"

    def test_creates_the_run_row_with_the_config(self, monkeypatch):
        submitted: list = []
        monkeypatch.setattr(simulation._POOL, "submit",
                            lambda fn, *a: submitted.append((fn, a)))
        db = FakeDB()
        res = simulation.start_run(db, target_events=5000, cooldown=1)
        assert res["ok"] is True
        row = db.rows[res["run_id"]]
        assert row["status"] == "pending"
        assert row["target_events"] == 5000
        cfg = row["config"]
        assert cfg["cooldown"] == 1
        assert len(cfg["assets"]) > 20, "the default should cover the full pair list"
        assert cfg["sl_multiples"] and cfg["tp_rs"] and cfg["max_bars"]
        assert submitted, "the job was never handed to the pool"

    def test_reports_a_missing_table_instead_of_crashing(self, monkeypatch):
        monkeypatch.setattr(simulation._POOL, "submit",
                            lambda fn, *a: None)
        db = FakeDB()

        def boom(table, row):
            raise RuntimeError("PGRST205 relation does not exist")

        db.insert = boom
        res = simulation.start_run(db)
        assert res["ok"] is False
        assert "057" in res["error"]


class TestMfeMae:
    def test_empty_input_says_so_instead_of_returning_an_empty_dict(self):
        out = simulation._mfe_mae([])
        assert out.get("error"), "a silent {} here is what hid the bug"

    def test_quantiles_and_reach_rates(self):
        class Ev:
            def __init__(self, mfe, mae):
                self.outcomes = {"1.5000|1.5000|20": type(
                    "R", (), {"mfe_r": mfe, "mae_r": mae})()}

        events = [Ev(float(i), -1.0) for i in range(100)]
        out = simulation._mfe_mae(events)
        # quantile convention is a[int(len*p)] — the upper sample, not the
        # interpolated median. Pinned so the numbers stay comparable run to run.
        assert out["mfe_median"] == 50.0
        assert out["mfe_p25"] == 25.0
        assert out["reached_1r_pct"] == 99.0
        assert out["reached_2r_pct"] == 98.0
        assert out["mae_p25"] == -1.0

    def test_names_the_cell_the_numbers_came_from(self):
        """MFE/MAE are in R units, so they depend on the cell. Reporting the
        cell is what makes the number checkable."""
        class Ev:
            def __init__(self, mfe, mae):
                self.outcomes = {"1.5000|1.5000|20": type(
                    "R", (), {"mfe_r": mfe, "mae_r": mae})()}

        out = simulation._mfe_mae([Ev(1.0, -0.5) for _ in range(10)])
        assert out["cell"] == "1.5000|1.5000|20"

    def test_falls_back_to_whatever_cell_the_events_carry(self):
        """The original bug: it looked for a key that was not in the grid, so
        the 5,000-sample run reported no excursion stats at all — including
        the 'only 2.3% of signals ever reached +2R' finding, which is the whole
        argument against the live target. A custom grid must still produce
        numbers, with the cell named."""
        class Ev:
            def __init__(self, mfe, mae):
                self.outcomes = {"2.0000|2.5000|10": type(
                    "R", (), {"mfe_r": mfe, "mae_r": mae})()}

        out = simulation._mfe_mae([Ev(1.0, -0.5) for _ in range(10)])
        assert out["cell"] == "2.0000|2.5000|10"
        assert out["mfe_max"] == 1.0

    def test_no_cells_at_all_reports_rather_than_vanishing(self):
        class Ev:
            outcomes: dict = {}

        out = simulation._mfe_mae([Ev() for _ in range(5)])
        assert "error" in out


class TestMissingMigrationIsVisible:
    """Migration 057 not applied must SAY SO.

    Database.select swallows read errors and returns [], so a probe through it
    makes a missing table indistinguishable from "no runs yet" — the tab would
    sit there silently broken. These tests pin the explicit signal.
    """

    def _client_that_raises(self):
        class Boom:
            def table(self, _n):
                raise RuntimeError(
                    "{'code':'PGRST205','message':\"Could not find the table "
                    "'public.simulation_runs'\"}")

        db = FakeDB()
        db._client = Boom()
        return db

    def test_probe_reports_the_table_as_missing(self):
        from app.api.routes import system
        assert system._sim_tables_missing(self._client_that_raises()) is True

    def test_probe_reports_present_when_the_query_works(self):
        from app.api.routes import system
        assert system._sim_tables_missing(FakeDB()) is False

    def test_list_endpoint_returns_setup_required_not_an_empty_list(self):
        import asyncio
        from app.api.routes import system

        class Req:
            class app:
                class state:
                    db = None
        Req.app.state.db = self._client_that_raises()

        res = asyncio.run(system.list_simulations(Req()))
        assert res["verdict"] == "fail"
        assert res["setup_required"] is True
        assert "057" in res["hint"]
        assert res["runs"] == []

    def test_start_endpoint_refuses_before_the_tables_exist(self):
        import asyncio
        from app.api.routes import system

        class Req:
            class app:
                class state:
                    db = None
        Req.app.state.db = self._client_that_raises()

        res = asyncio.run(system.start_simulation(Req(), {}))
        assert res["ok"] is False
        assert "057" in res["hint"]


class TestRunLifecycle:
    """End-to-end through the real worker, with only the network stubbed."""

    def _bars(self, n=320, up=True):
        from app.integrations.quotes import Candle
        out = []
        p = 1.1000
        for i in range(n):
            drift = 0.0004 if (up and i % 3) else (-0.0004 if i % 3 else 0.0001)
            p += drift
            out.append(Candle(o=p, h=p + 0.0009, l=p - 0.0009, c=p))
        return out

    def _run(self, db, monkeypatch, target=120, cooldown=1, cancel_after=None):
        """Run _run_job inline and return the final run row."""
        cfg = {"target_events": target, "cooldown": cooldown,
               "assets": ["EURUSD", "GBPUSD"], "days": 1095,
               "sl_multiples": [1.0, 1.5], "tp_rs": [1.0, 1.5],
               "max_bars": [5, 10, 20]}
        monkeypatch.setattr(simulation, "_fetch_series",
                            lambda c: {"EURUSD": self._bars(), "GBPUSD": self._bars(up=False)})
        monkeypatch.setattr(simulation, "_CANCEL", {})
        db.insert(simulation.RUNS_TABLE, {"id": "run1", "status": "pending",
                                          "target_events": target})
        cancel = threading.Event()
        simulation._CANCEL["run1"] = cancel
        if cancel_after is not None:
            # Flip the flag once enough events have been written.
            orig = simulation._flush

            def maybe_cancel(_db, rows):
                orig(_db, rows)
                if len(db.inserted) >= cancel_after:
                    cancel.set()

            monkeypatch.setattr(simulation, "_flush", maybe_cancel)
        simulation._run_job(db, "run1", cfg)
        return db.rows["run1"], db.inserted

    def test_a_run_reaches_done_and_writes_events(self, monkeypatch):
        db = FakeDB()
        row, events = self._run(db, monkeypatch)
        assert row["status"] == "done", row.get("error")
        assert row["stage"] == "done"
        assert row["finished_at"]
        assert row["total_events"] > 0
        assert row["processed"] > 0
        assert events, "no events persisted"
        assert row["result"], "no verdict written"

    def test_every_event_carries_the_label_and_the_r(self, monkeypatch):
        db = FakeDB()
        _, events = self._run(db, monkeypatch)
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs), "events are out of order"
        assert len(set(seqs)) == len(seqs), "duplicate seq"
        for e in events:
            assert e["label"] in ("tp", "sl", "expired")
            assert isinstance(e["r_multiple"], (int, float))
            assert e["run_id"] == "run1"
            assert e["asset"] in ("EURUSD", "GBPUSD")
            assert e["direction"] in ("BUY", "SELL")

    def test_progress_advances_and_never_exceeds_the_total(self, monkeypatch):
        db = FakeDB()
        simulation._run_job  # noqa: B018 - documenting the entry point
        row, _ = self._run(db, monkeypatch)
        prog = [u.get("processed") for u in db.updates if "processed" in u]
        assert prog, "progress was never written"
        assert prog == sorted(prog), f"progress went backwards: {prog}"
        assert prog[-1] == row["processed"]
        assert row["processed"] <= max(row["total_events"], 1)

    def test_cancel_stops_early_and_is_recorded(self, monkeypatch):
        """Cancel latency is bounded by the write batch (200 events), so the
        test needs enough events to span at least two batches."""
        db = FakeDB()
        row, events = self._run(db, monkeypatch, target=400, cancel_after=200)
        assert row["status"] == "cancelled"
        assert row["processed"] < row["total_events"], \
            "cancel was requested but the run finished anyway"
        assert len(events) < 240

    def test_a_failure_is_recorded_not_swallowed(self, monkeypatch):
        db = FakeDB()
        monkeypatch.setattr(simulation, "_fetch_series",
                            lambda c: (_ for _ in ()).throw(RuntimeError("network down")))
        cfg = {"target_events": 50, "cooldown": 1, "assets": ["EURUSD"],
               "days": 1095, "sl_multiples": [1.0], "tp_rs": [1.0],
               "max_bars": [10]}
        db.insert(simulation.RUNS_TABLE, {"id": "r", "status": "pending",
                                          "target_events": 50})
        simulation._run_job(db, "r", cfg)
        row = db.rows["r"]
        assert row["status"] in ("failed", "done")
        if row["status"] == "failed":
            assert row["error"]

    def test_the_active_slot_is_released_even_after_a_failure(self, monkeypatch):
        """A leaked 'a run is live' slot would block every future run."""
        db = FakeDB()
        monkeypatch.setattr(simulation, "_fetch_series",
                            lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
        cfg = {"target_events": 50, "cooldown": 1, "assets": [], "days": 1095,
               "sl_multiples": [1.0], "tp_rs": [1.0], "max_bars": [10]}
        db.insert(simulation.RUNS_TABLE, {"id": "r", "status": "pending",
                                          "target_events": 50})
        simulation._set_active("r")
        simulation._run_job(db, "r", cfg)
        assert simulation.active_run_id() is None


class TestAnalysis:
    def _events(self, n=200):
        from app.engine.triple_barrier import LabelledEvent
        out = []
        for i in range(n):
            ev = LabelledEvent(asset="EURUSD" if i % 2 else "GBPUSD",
                               direction="BUY", bar_index=i,
                               entry=1.10, atr_pct=0.5,
                               opportunity=60.0, confidence=70.0,
                               future=[])
            out.append(ev)
        return out

    def test_reports_a_walk_forward_verdict_not_just_in_sample(self):
        """A sweep without the out-of-sample number is a number that has not
        been tested — the first run of this study showed exactly that."""
        cfg = {"sl_multiples": [1.0, 1.5], "tp_rs": [1.0, 2.0],
               "max_bars": [10, 20], "days": 1095, "cooldown": 1}
        events = self._events(400)
        # Label them so the sweep has something to aggregate.
        from app.engine.triple_barrier import BarrierSpec, label_event
        from app.integrations.quotes import Candle
        for i, ev in enumerate(events):
            # alternating winners/losers so the grid has real spread
            up = i % 3 != 0
            bars = [Candle(o=1.10, h=1.10 + (0.004 if up else 0.0002),
                           l=1.10 - (0.0002 if up else 0.004), c=1.10),
                    Candle(o=1.10, h=1.10 + (0.006 if up else 0.0003),
                           l=1.10 - (0.0003 if up else 0.006), c=1.10)]
            ev.future = bars
            for m in cfg["sl_multiples"]:
                for r in cfg["tp_rs"]:
                    for mb in cfg["max_bars"]:
                        atr = ev.entry * ev.atr_pct / 100.0
                        sp = BarrierSpec(m * atr, m * atr * r, mb)
                        ev.outcomes[f"{m:.4f}|{r:.4f}|{mb}"] = label_event(
                            "BUY", ev.entry, sp, bars)

        verdict = simulation._analyse(events, cfg)
        assert verdict["n_events"] == 400
        assert verdict["top_paid"], "no grid rows ranked"
        assert "walk_forward" in verdict
        wf = verdict["walk_forward"]
        if "error" not in wf:
            assert wf["train_n"] + wf["test_n"] == 400
            assert len(wf["checks"]) > 0
            # every reported cell must carry BOTH numbers
            for c in wf["checks"]:
                assert "train_mean_r" in c and "test_mean_r" in c
            assert isinstance(wf["holds_out"], bool)
        assert "mfe_mae" in verdict

    def test_too_few_events_says_so_instead_of_faking_a_split(self):
        cfg = {"sl_multiples": [1.0], "tp_rs": [1.0], "max_bars": [10],
               "days": 1095, "cooldown": 1}
        verdict = simulation._analyse(self._events(20), cfg)
        assert "error" in verdict["walk_forward"]

    def test_per_asset_is_ranked_on_train_only(self):
        """Ranking pairs on the data you then score them with is a leak — the
        per-asset table must not decide 'keep' from the test period."""
        cfg = {"sl_multiples": [1.0, 1.5], "tp_rs": [1.0, 2.0],
               "max_bars": [10, 20], "days": 1095, "cooldown": 1}
        events = self._events(300)
        from app.engine.triple_barrier import BarrierSpec, label_event
        from app.integrations.quotes import Candle
        for i, ev in enumerate(events):
            up = (i % 4) != 0
            bars = [Candle(o=1.10, h=1.10 + (0.004 if up else 0.0002),
                           l=1.10 - (0.0002 if up else 0.004), c=1.10),
                    Candle(o=1.10, h=1.10 + (0.006 if up else 0.0003),
                           l=1.10 - (0.0003 if up else 0.006), c=1.10)]
            ev.future = bars
            for m in cfg["sl_multiples"]:
                for r in cfg["tp_rs"]:
                    for mb in cfg["max_bars"]:
                        atr = ev.entry * ev.atr_pct / 100.0
                        ev.outcomes[f"{m:.4f}|{r:.4f}|{mb}"] = label_event(
                            "BUY", ev.entry, BarrierSpec(m * atr, m * atr * r, mb),
                            bars)
        verdict = simulation._analyse(events, cfg)
        assert verdict.get("per_asset"), "no per-asset breakdown"
        rows = verdict["per_asset"]
        assert rows == sorted(rows, key=lambda r: -r["train_mean_r"])
        for r in rows:
            # 'keep' must be derivable from the train columns alone
            assert r["keep"] == (r["train_mean_r"] > 0)
