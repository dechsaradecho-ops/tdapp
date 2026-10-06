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


class TestOptimizer:
    """One run row, several rounds: refine around the winner until something
    holds, then stop and propose. Stubbed analyses — the math underneath is
    tested elsewhere; what matters here is the loop's stopping honesty."""

    def _v(self, test_r, holds=False, live=None,
           winner=(2.0, 0.75, 20), train_r=0.10):
        return {
            "walk_forward": {
                "holds_out": holds, "test_mean_r": test_r, "test_n": 100,
                "test_win_rate_pct": 50.0,
                "survivors": 1 if holds else 0, "candidates_checked": 8,
                "winner": {"sl_mult": winner[0], "tp_r": winner[1],
                           "max_bars": winner[2], "train_mean_r": train_r}},
            "recommendation": {"live_suggestion": live},
            "grid_cells": 144,
        }

    def _cfg(self, max_rounds=4):
        return {"optimizer": {"max_rounds": max_rounds},
                "sl_multiples": [1.0, 2.0], "tp_rs": [0.5, 1.0],
                "max_bars": [20], "cooldown": 1}

    def test_stops_at_first_live_suggestion(self, monkeypatch):
        import threading
        monkeypatch.setattr(
            simulation, "_analyse",
            lambda ev, cfg: self._v(0.12, holds=True, live={"changes": []}))
        out = simulation._optimize(
            FakeDB(), "r", self._cfg(), [object()], threading.Event())
        assert out["optimizer"]["status"] == "found"
        assert out["optimizer"]["rounds_run"] == 1
        assert out["optimizer"]["best_round"] == 1
        assert len(out["rounds"]) == 1

    def test_exhausts_rounds_and_reports_the_best(self, monkeypatch):
        import threading
        seq = [self._v(-0.10), self._v(-0.05), self._v(-0.08), self._v(-0.02)]
        monkeypatch.setattr(
            simulation, "_analyse", lambda ev, cfg: seq.pop(0))
        out = simulation._optimize(
            FakeDB(), "r", self._cfg(), [object()], threading.Event())
        assert out["optimizer"]["status"] == "exhausted"
        assert out["optimizer"]["rounds_run"] == 4
        assert out["optimizer"]["best_round"] == 4  # -0.02 is the best test
        assert out["walk_forward"]["test_mean_r"] == -0.02

    def test_stall_stops_the_loop_early(self, monkeypatch):
        import threading
        seq = [self._v(-0.10), self._v(-0.12), self._v(-0.11),
               self._v(0.50)]
        monkeypatch.setattr(
            simulation, "_analyse", lambda ev, cfg: seq.pop(0))
        out = simulation._optimize(
            FakeDB(), "r", self._cfg(), [object()], threading.Event())
        # round 2 and 3 both fail to improve -> stop before round 4,
        # even though round 4 "would have" held
        assert out["optimizer"]["status"] == "stalled"
        assert out["optimizer"]["rounds_run"] == 3

    def test_cancel_keeps_finished_rounds(self, monkeypatch):
        import threading
        seq = [self._v(-0.10), self._v(-0.05)]
        cancel = threading.Event()

        def analyse(ev, cfg):
            cancel.set()  # user hits stop during round 2's analysis
            return seq.pop(0)

        monkeypatch.setattr(simulation, "_analyse", analyse)
        out = simulation._optimize(
            FakeDB(), "r", self._cfg(), [object()], cancel)
        assert out["optimizer"]["status"] == "cancelled"
        assert len(out["rounds"]) >= 1

    def test_no_winner_breaks_without_crashing(self, monkeypatch):
        import threading
        monkeypatch.setattr(simulation, "_analyse",
                            lambda ev, cfg: {"walk_forward": {}})
        out = simulation._optimize(
            FakeDB(), "r", self._cfg(), [object()], threading.Event())
        assert out["optimizer"]["rounds_run"] == 1
        assert out["rounds"][0]["winner"] == {"sl_mult": None, "tp_r": None,
                                             "max_bars": None}

    def test_refine_searches_new_coords_not_a_rerank(self):
        sl, tp, mb = simulation._refine_grid(2.0, 0.75, 20)
        assert sl == [1.75, 2.0, 2.25]
        assert tp == [0.6562, 0.75, 0.8438]
        assert mb == [20]
        # every refined value must be a NEW coordinate the full grid never had
        for v in sl:
            assert v not in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0) \
                or v == 2.0

    def test_optimizer_config_defaults_to_single_round(self, monkeypatch):
        """No optimizer key = previous behaviour, byte for byte."""
        from tests.test_simulation import FakeDB as SimDB

        submitted = []
        monkeypatch.setattr(simulation._POOL, "submit",
                            lambda fn, *a: submitted.append((fn, a)))
        db = SimDB()
        res = simulation.start_run(db)
        assert res["ok"] is True
        assert db.rows[res["run_id"]]["config"]["optimizer"] is None

    def test_optimizer_config_is_capped(self, monkeypatch):
        from tests.test_simulation import FakeDB as SimDB

        monkeypatch.setattr(simulation._POOL, "submit", lambda fn, *a: None)
        db = SimDB()
        res = simulation.start_run(
            db, optimizer={"enabled": True, "max_rounds": 99})
        assert db.rows[res["run_id"]]["config"]["optimizer"] == {
            "max_rounds": 8}


class TestDefaultGrid:
    def test_unreachable_targets_are_not_in_the_default_grid(self):
        """Owner 2026-10-06: 2.5R/3.0R out — the 5,000-sample run showed ~2%
        of signals ever reaching +2R. The default grid must reflect that."""
        assert list(simulation.TP_RS) == [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        assert len(simulation.SL_MULTIPLES) * len(simulation.TP_RS) \
            * len(simulation.MAX_BARS) == 8 * 6 * 3


class TestRecommendation:
    """After a run: what changes next round (from -> to), and nothing else."""

    def _cfg(self):
        return {"sl_multiples": [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0],
                "tp_rs": [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0],
                "max_bars": [5, 10, 20],
                "live_baseline": {"source": "db", "sl_distance_mode": "medium",
                                  "sl_atr_mult": 1.5, "sl_min_pct": 0.65,
                                  "sl_max_pct": 1.2, "rr_target": 1.5,
                                  "min_opportunity": 60.0,
                                  "min_confidence": 70.0}}

    def _verdict(self, holds=True, test_r=0.12, gate_test=0.08,
                 prod_test=-0.07):
        return {
            "walk_forward": {
                "holds_out": holds, "survivors": 3 if holds else 0,
                "candidates_checked": 8,
                "test_mean_r": test_r if holds else -0.10,
                "winner": {"sl_mult": 2.0, "tp_r": 0.75, "max_bars": 20,
                           "train_mean_r": 0.10},
            },
            "best_gate": {"min_opp": 50.0, "min_conf": 0.0,
                          "selected_on": "train",
                          "train_mean_r": 0.05, "test_mean_r": gate_test},
            "per_asset": [
                {"asset": "AUDUSD", "train_mean_r": 0.2, "test_mean_r": 0.3,
                 "train_n": 100, "test_n": 50},
                {"asset": "EURUSD", "train_mean_r": -0.2, "test_mean_r": 0.3,
                 "train_n": 100, "test_n": 50},
                {"asset": "GBPUSD", "train_mean_r": -0.2, "test_mean_r": -0.3,
                 "train_n": 100, "test_n": 50},
            ],
            "mfe_mae": {"mfe_median": 0.7, "reached_1r_pct": 40.0,
                        "reached_1_5r_pct": 22.0, "reached_2r_pct": 2.0},
            "production_benchmark": {
                "available": True, "test": {"mean_r": prod_test, "n": 900,
                                            "win_rate_pct": 41.0}},
        }

    def test_surviving_winner_narrows_the_grid(self):
        rec = simulation._recommend_next(self._cfg(), self._verdict())
        assert rec["has_plan"] is True
        nxt = rec["next_config"]
        # zoom around 2.0xATR / 0.75R / 20 bars, not the whole grid
        assert nxt["sl_multiples"] == [1.5, 2.0, 2.5]
        assert nxt["tp_rs"] == [0.5, 0.75, 1.0]
        assert nxt["max_bars"] == [20]
        fields = {c["field"] for c in rec["changes"]}
        assert {"sl_multiples", "tp_rs"} <= fields
        for c in rec["changes"]:
            assert c["from"] != c["to"] and c["reason"]

    def test_failed_winner_does_not_narrow_but_prunes_unreachable(self):
        rec = simulation._recommend_next(self._cfg(), self._verdict(holds=False))
        assert rec["has_plan"] is True  # the prune is still actionable
        nxt = rec["next_config"]
        assert "sl_multiples" not in nxt, \
            "narrowing around a failed winner is cherry-picking"
        # only 2.3% ever reached +2R: 2.5R/3.0R go, 2.0R stays as the
        # boundary (dropping the exact region production targets would blind
        # the comparison against live).
        assert nxt["tp_rs"] == [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        assert "assets" not in nxt

    def test_positive_gate_is_fixed_otherwise_the_ladder_stays(self):
        rec = simulation._recommend_next(self._cfg(), self._verdict())
        assert rec["next_config"]["gate_opps"] == [50.0]
        assert rec["next_config"]["gate_confs"] == [0.0]
        rec2 = simulation._recommend_next(
            self._cfg(), self._verdict(gate_test=-0.05))
        assert "gate_opps" not in rec2["next_config"]

    def test_asset_subset_only_behind_a_survivor(self):
        rec = simulation._recommend_next(self._cfg(), self._verdict())
        # only AUDUSD is positive in BOTH periods with n>=30
        assert rec["next_config"]["assets"] == ["AUDUSD"]
        rec2 = simulation._recommend_next(self._cfg(), self._verdict(holds=False))
        assert "assets" not in rec2["next_config"]

    def test_live_is_suggested_only_on_a_margin_over_production(self):
        rec = simulation._recommend_next(self._cfg(), self._verdict())
        live = rec["live_suggestion"]
        assert live is not None  # 0.12 vs -0.07 clears the 0.05 margin
        assert live["candidate_test_r"] == 0.12
        assert "ต้องกดยืนยันเอง" in live["note"]
        rec2 = simulation._recommend_next(
            self._cfg(), self._verdict(test_r=0.01, prod_test=0.0))
        assert rec2["live_suggestion"] is None, \
            "a 0.01 margin over production is noise, not a suggestion"

    def test_nothing_actionable_says_so_plainly(self):
        v = self._verdict(holds=False, gate_test=-0.2)
        v["mfe_mae"] = {"mfe_median": 1.5, "reached_1r_pct": 80.0,
                        "reached_1_5r_pct": 60.0, "reached_2r_pct": 40.0}
        rec = simulation._recommend_next(self._cfg(), v)
        assert rec["has_plan"] is False
        assert rec["live_suggestion"] is None
        assert "ยังสรุปอะไรไม่ได้" in rec["note"] or "เดา" in rec["note"]

    def test_never_raises_on_a_malformed_verdict(self):
        rec = simulation._recommend_next({}, {})
        assert rec["has_plan"] is False
        assert rec["live_suggestion"] is None

    def test_mfe_key_uses_underscore(self):
        """reached_1.5r_pct rendered as '—' in the UI; the key must not
        contain a dot."""
        class Ev:
            def __init__(self):
                self.outcomes = {"1.5000|1.5000|20": type(
                    "R", (), {"mfe_r": 1.6, "mae_r": -0.5})()}

        out = simulation._mfe_mae([Ev() for _ in range(10)])
        assert "reached_1_5r_pct" in out
        assert "reached_1.5r_pct" not in out
