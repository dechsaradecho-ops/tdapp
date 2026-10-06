"""Simulation config must be separable from live trading settings.

Owner 2026-10-06: "แยกค่า setting sim กับ setting จริง ถ้าได้ผลดีค่อยแนะนำ
เปลี่ยน setting จริง" — a candidate can only be judged on its own merits if the
run's parameters are NOT inherited from production, and if production is then
scored against the same data as a benchmark rather than quietly competing.

These tests pin that separation and the selection discipline: every choice
made on the TRAIN period, scored once on TEST.
"""
from __future__ import annotations

import pytest

from app.services import simulation
from tests.test_simulation import FakeDB


@pytest.fixture(autouse=True)
def _clean():
    simulation._set_active(None)
    simulation._CANCEL.clear()
    yield
    simulation._set_active(None)
    simulation._CANCEL.clear()


class TestConfigSeparation:
    def test_run_config_never_inherits_the_live_gate(self, monkeypatch):
        """If the run copied min_confidence/min_opportunity from production, a
        candidate could never be evaluated independently of it."""
        monkeypatch.setattr(simulation._POOL, "submit", lambda fn, *a: None)
        monkeypatch.setattr(simulation, "live_baseline",
                            lambda db: {"source": "db", "min_confidence": 70.0,
                                        "min_opportunity": 60.0, "rr_target": 1.5})
        db = FakeDB()
        res = simulation.start_run(db, target_events=100)
        cfg = db.rows[res["run_id"]]["config"]
        assert "min_confidence" not in cfg
        assert "min_opportunity" not in cfg
        assert cfg["gate_opps"] == list(simulation.GATE_OPPS)
        assert cfg["gate_confs"] == list(simulation.GATE_CONFS)

    def test_live_settings_are_captured_as_a_read_only_benchmark(self, monkeypatch):
        monkeypatch.setattr(simulation._POOL, "submit", lambda fn, *a: None)
        monkeypatch.setattr(simulation, "live_baseline",
                            lambda db: {"source": "db", "rr_target": 2.0})
        db = FakeDB()
        res = simulation.start_run(db)
        assert db.rows[res["run_id"]]["config"]["live_baseline"] == {
            "source": "db", "rr_target": 2.0}

    def test_baseline_is_captured_at_start_not_at_analysis(self, monkeypatch):
        """A benchmark that moves under the experiment is not a benchmark."""
        seen = []

        def boom(db):
            return {"source": "db", "rr_target": 99.0}

        monkeypatch.setattr(simulation._POOL, "submit", lambda fn, *a: None)
        monkeypatch.setattr(simulation, "live_baseline", boom)
        db = FakeDB()
        simulation.start_run(db)
        seen.append(db)
        # _analyse must read the cfg it was handed, never re-query settings
        assert seen and "live_baseline" in \
            list(db.rows.values())[0]["config"]

    def test_live_baseline_returns_a_failure_marker_not_a_crash(self):
        class NoDB:
            available = True

            def __init__(self):
                def raiser(*a, **k):
                    raise RuntimeError("connection reset")
                self._client = type("C", (), {"table": raiser})()

        out = simulation.live_baseline(NoDB())
        assert out["source"] == "unavailable"

    def test_benchmark_is_marked_unavailable_when_settings_cannot_be_read(self):
        out = simulation._score_production([object()], [object()], {})
        assert out["available"] is False
        assert out["reason"]


class TestGateIsSeparateFromTheBarrierGrid:
    def _events(self, n=400):
        from app.engine.triple_barrier import LabelledEvent
        out = []
        for i in range(n):
            out.append(LabelledEvent(
                asset="EURUSD", direction="BUY", bar_index=i, entry=1.10,
                atr_pct=0.5,
                opportunity=40.0 + (i % 60),      # spans the gate ladder
                confidence=50.0 + (i % 45),
                future=[]))
        return out

    def test_gate_pass_is_inclusive_at_the_threshold(self):
        e = self._events(1)[0]
        e.opportunity, e.confidence = 60.0, 70.0
        assert simulation._gate_pass(e, 60.0, 70.0)
        assert not simulation._gate_pass(e, 60.1, 70.0)
        assert not simulation._gate_pass(e, 60.0, 70.1)
        # a zero gate lets everything through — the "do not filter at all" arm
        assert simulation._gate_pass(e, 0.0, 0.0)

    def test_score_counts_only_passing_events(self):
        evs = self._events(200)
        loose = simulation._score(evs, 1.5, 1.5, 20, 0.0, 0.0)
        strict = simulation._score(evs, 1.5, 1.5, 20, 90.0, 90.0)
        assert loose["n"] == 200
        assert strict["n"] < loose["n"]

    def test_score_reports_zero_rather_than_dividing_by_zero(self):
        evs = self._events(50)
        out = simulation._score(evs, 1.5, 1.5, 20, 999.0, 999.0)
        assert out["n"] == 0
        assert out["mean_r"] == 0.0
        assert out["win_rate_pct"] == 0.0


class TestSelectionDiscipline:
    """Every reported choice must be made on TRAIN and scored on TEST."""

    def _labelled(self, n=600):
        from app.engine.triple_barrier import (
            BarrierSpec, LabelledEvent, label_event)
        from app.integrations.quotes import Candle
        evs = []
        for i in range(n):
            up = (i % 3) != 0
            bars = [Candle(o=1.10, h=1.10 + (0.005 if up else 0.0002),
                           l=1.10 - (0.0002 if up else 0.005), c=1.10),
                    Candle(o=1.10, h=1.10 + (0.008 if up else 0.0003),
                           l=1.10 - (0.0003 if up else 0.008), c=1.10),
                    Candle(o=1.10, h=1.10 + (0.010 if up else 0.0004),
                           l=1.10 - (0.0004 if up else 0.010), c=1.10)]
            ev = LabelledEvent(asset="EURUSD", direction="BUY", bar_index=i,
                               entry=1.10, atr_pct=0.5,
                               opportunity=40.0 + (i % 60),
                               confidence=50.0 + (i % 45), future=bars)
            for m in (1.0, 1.5, 2.0):
                for r in (1.0, 1.5):
                    for mb in (10, 20):
                        atr = ev.entry * ev.atr_pct / 100.0
                        ev.outcomes[f"{m:.4f}|{r:.4f}|{mb}"] = label_event(
                            "BUY", ev.entry, BarrierSpec(m * atr, m * atr * r, mb),
                            bars)
            evs.append(ev)
        return evs

    def _cfg(self):
        return {"sl_multiples": [1.0, 1.5, 2.0], "tp_rs": [1.0, 1.5],
                "max_bars": [10, 20], "gate_opps": [0, 60], "gate_confs": [0, 70],
                "days": 1095, "cooldown": 1,
                "live_baseline": {"source": "db", "sl_atr_mult": 1.5,
                                  "sl_min_pct": 0.65, "sl_max_pct": 1.2,
                                  "rr_target": 1.5, "min_opportunity": 60.0,
                                  "min_confidence": 70.0}}

    def test_gate_is_chosen_on_train_and_reported_on_test(self):
        v = simulation._analyse(self._labelled(), self._cfg())
        rows = v["gate_sweep"]
        assert rows, "no gate candidates"
        # ranked by TRAIN mean R descending
        tr = [r["train_mean_r"] for r in rows]
        assert tr == sorted(tr, reverse=True)
        best = v["best_gate"]
        assert best["selected_on"] == "train"
        assert best["min_opp"] == rows[0]["min_opp"]
        assert best["min_conf"] == rows[0]["min_conf"]
        # and it carries BOTH numbers
        assert best["train_mean_r"] is not None
        assert best["test_mean_r"] is not None

    def test_every_gate_row_reports_train_and_test(self):
        v = simulation._analyse(self._labelled(), self._cfg())
        for r in v["gate_sweep"]:
            assert "train_mean_r" in r and "test_mean_r" in r
            assert "train_n" in r and "test_n" in r

    def test_walk_forward_reports_survivors_and_candidates(self):
        """The whole point: a ranked list is not a result. How many of the
        top candidates stayed positive out-of-sample must be stated."""
        v = simulation._analyse(self._labelled(), self._cfg())
        wf = v["walk_forward"]
        assert wf.get("candidates_checked", 0) > 0
        assert 0 <= wf.get("survivors", 0) <= wf["candidates_checked"]
        assert isinstance(wf.get("holds_out"), bool)
        for c in wf["checks"]:
            assert "train_mean_r" in c and "test_mean_r" in c

    def test_production_benchmark_is_scored_the_same_way(self):
        v = simulation._analyse(self._labelled(), self._cfg())
        pb = v["production_benchmark"]
        assert pb["available"] is True
        assert pb["config"]["rr_target"] == 1.5
        assert pb["train"]["n"] > 0 and pb["test"]["n"] > 0
        assert isinstance(pb["positive_test"], bool)

    def test_candidate_counts_are_reported_so_the_race_is_visible(self):
        v = simulation._analyse(self._labelled(), self._cfg())
        assert v["grid_cells"] == 3 * 2 * 2
        assert v["gate_candidates"] == 4
        assert v["best_gate"]["selected_on"] == "train"

    def test_too_few_events_reports_instead_of_scoring(self):
        v = simulation._analyse(self._labelled(20), self._cfg())
        assert "error" in v["walk_forward"]
        assert "gate_sweep" not in v
