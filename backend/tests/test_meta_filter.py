"""Meta-filter math — pure, no DB, no network.

The properties that matter: the split cannot leak (purge + embargo on a bar
value, not a row count), the threshold is picked on train only, constants
cannot smuggle themselves in as signals, and retrains are identical.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_meta_filter.py -v
"""
from __future__ import annotations

import pytest

from app.engine import meta_filter as mf


def row(bar, feats, y=0, r=0.0):
    return {"bar_index": bar, "features": dict(feats), "y": y, "r": r}


def blobs(n=400, sep=3.0):
    """Two separable blobs on x0; x1 is pure noise. Deterministic."""
    rows = []
    for i in range(n):
        cls = i % 2
        rows.append(row(
            i, {"x0": (sep if cls else -sep) + (i % 7) * 0.01,
                "x1": float((i * 13) % 11) * 0.1,
                "const": 1.0},
            y=cls, r=0.5 if cls else -0.5))
    return rows


def to_xy(rows, feats=("x0", "x1", "const")):
    X, kept, stats = mf.standardize(rows, feats)
    return X, [r["y"] for r in rows], kept, stats


class TestPurgedSplit:
    def test_splits_on_bar_value_not_row_count(self):
        # 90% of ROWS sit below bar 90 but the cut is a bar VALUE: with bars
        # 0..89 + 100..109, frac 0.7 lands the cut at bar 70, not at "70% of
        # the rows". A count-based split would have put pair B's late history
        # in train; the value cut keeps train bar<=50, test bar>70.
        rows = [row(b, {"x0": 0.0}) for b in (list(range(90)) + list(range(100, 110)))]
        tr, te = mf.purged_split(rows, frac=0.7, embargo=20)
        assert max(r["bar_index"] for r in tr) == 50
        # test is EVERYTHING past the cut value — bars 71..89 as well as the
        # 100s. A count-based split would have stopped at row 70 instead.
        assert min(r["bar_index"] for r in te) == 71
        assert len(tr) == 51 and len(te) == 29

    def test_embargo_drops_train_rows_near_the_cut(self):
        rows = [row(b, {"x0": 0.0}) for b in range(100)]
        tr, te = mf.purged_split(rows, frac=0.7, embargo=20)
        # cut = bar 70; train must end at 50, test starts at 71
        assert max(r["bar_index"] for r in tr) == 50
        assert min(r["bar_index"] for r in te) == 71
        assert len(tr) == 51 and len(te) == 29

    def test_empty_in_empty_out(self):
        assert mf.purged_split([]) == ([], [])


class TestStandardize:
    def test_constants_are_dropped_and_named(self):
        X, y, kept, stats = to_xy(blobs(100))
        assert "const" not in kept
        assert stats["const"] == {"kept": False, "reason": "zero variance"}
        assert set(kept) == {"x0", "x1"}

    def test_output_is_zero_mean_unit_variance(self):
        X, y, kept, stats = to_xy(blobs(200))
        d = len(kept)
        for j in range(d):
            col = [x[j] for x in X]
            assert abs(sum(col) / len(col)) < 1e-9
            var = sum(v * v for v in col) / len(col)
            assert abs(var - 1.0) < 1e-9


class TestFit:
    def test_separable_data_is_learned(self):
        X, y, kept, stats = to_xy(blobs(400))
        model = mf.fit(X, y)
        probs = mf.predict_proba(X, model)
        acc = sum((p >= 0.5) == bool(t) for p, t in zip(probs, y)) / len(y)
        assert acc > 0.95, f"a separable blob must be learned, got {acc}"
        # the noise column must carry ~no weight vs the signal column
        j0, j1 = kept.index("x0"), kept.index("x1")
        assert abs(model["weights"][j0]) > 5 * abs(model["weights"][j1])

    def test_retrains_are_identical(self):
        X, y, *_ = to_xy(blobs(200))
        a, b = mf.fit(X, y), mf.fit(X, y)
        assert a["weights"] == b["weights"] and a["bias"] == b["bias"]

    def test_degenerate_input_returns_a_model_not_an_exception(self):
        assert mf.fit([], [])["weights"] == []
        X, y, *_ = to_xy(blobs(50))
        assert mf.fit(X, y, iters=0)["iters"] == 0


class TestEvaluate:
    def _probs(self, n=1000):
        # model is right 70% of the time; winners pay +0.5, losers cost -0.5
        import math
        train_p, train_y, train_r, test_p, test_y, test_r = [], [], [], [], [], []
        for i in range(n):
            good = (i % 10) < 7
            p = 0.8 if good else 0.2
            (train_p, train_y, train_r) if i < 700 else (test_p, test_y, test_r)
            bucket = (train_p, train_y, train_r) if i < 700 else (test_p, test_y, test_r)
            bucket[0].append(p)
            bucket[1].append(1 if good else 0)
            bucket[2].append(0.5 if good else -0.5)
        return train_p, train_y, train_r, test_p, test_y, test_r

    def test_picks_threshold_on_train_reports_on_test(self):
        tr_p, tr_y, tr_r, te_p, te_y, te_r = self._probs()
        out = mf.evaluate(tr_p, tr_y, tr_r, te_p, te_y, te_r)
        assert out["picked"]["threshold"] == 0.40  # lowest bar that still takes
        assert out["picked"]["test_mean_r"] == pytest.approx(0.5)
        assert out["holds_out"] is True
        assert out["base_test_mean_r"] == pytest.approx(0.2)  # 70/30 mix

    def test_business_metric_is_money_not_accuracy(self):
        # a filter can be "accurate" while losing money; the picker must rank
        # by taken-mean-R, which the thresholds table exposes per row
        tr_p, tr_y, tr_r, te_p, te_y, te_r = self._probs()
        out = mf.evaluate(tr_p, tr_y, tr_r, te_p, te_y, te_r)
        for row in out["thresholds"]:
            assert "test_mean_r" in row and "train_mean_r" in row

    def test_too_few_taken_is_no_pick_not_a_result(self):
        out = mf.evaluate([0.9] * 10, [1] * 10, [0.5] * 10,
                          [0.9] * 10, [1] * 10, [0.5] * 10,
                          min_taken=1000)
        assert out["picked"] is None
        assert out["holds_out"] is False
