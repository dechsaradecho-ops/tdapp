"""Meta-filter: predict which signals go nowhere, so the engine can skip them.

WHY (owner 2026-10-06)
----------------------
Three 5,000-sample runs agree: the signal has no measurable edge, and the MFE
distribution says 3 in 4 signals never reach +1.5R. Tuning SL/TP cannot fix a
signal that offers nothing — the only remaining lever on the SAME generator
is SELECTION: take fewer trades, only the ones likely to move.

This module does NOT predict direction (the strategy engine already emits a
side). It predicts P(y=1) = P(the primary-cell trade ends profitable) from
entry-time features only — classic meta-labeling (Lopez de Prado). A
threshold on that probability becomes the "take / skip" gate.

WHY HAND-ROLLED
---------------
The backend has no numpy/scikit (requirements.txt carries neither, and the
deploy must not gain a heavy dependency for a 9-feature model). Full-batch
gradient descent on standardized features converges in a few hundred
iterations at n=5,000 — milliseconds per iteration in pure Python. Zero
initial weights + fixed order = bit-identical retrains; there is no seed to
forget.

DISCIPLINE (same as the barrier study)
--------------------------------------
* features are entry-time only — no lookahead by construction,
* the threshold is picked on TRAIN (max taken-mean-R, min 100 taken),
* the reported numbers are TEST-only, after a purged split with an embargo
  of the label horizon (events whose outcome windows overlap the test period
  leave the train set — without purging, shared future bars leak).
"""

from __future__ import annotations

import math
from typing import Any, Optional

__all__ = [
    "FEATURES",
    "purged_split",
    "standardize",
    "fit",
    "predict_proba",
    "evaluate",
]

#: Entry-time features, in a fixed order. Must match META_FEATURES in
#: app/services/simulation.py (which mirrors event_features, minus the
#: string `regime`). Near-constant columns are dropped at standardize time
#: with a note — a zero-variance feature teaches nothing and only adds noise.
FEATURES = ("adx", "rsi", "macd_hist", "atr_pct", "volatility_index",
            "chg20", "ema_gap_atr", "st_agree", "ema_agree", "macd_agree")


def _sigmoid(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def purged_split(rows: list[dict], frac: float = 0.7,
                 embargo: int = 20) -> tuple[list[dict], list[dict]]:
    """Chronological split with purge + embargo.

    Rows carry `bar_index` (position on a shared timeline). The cut is a
    bar_index VALUE, not a row count — splitting by count would put pair A's
    early history against pair B's late one (the same asset-grouping trap
    that once silently broke the study's per-asset table). Train rows at or
    above (cut - embargo) are dropped: their outcome windows reach into the
    test period and would leak through the shared future bars.
    """
    if not rows:
        return [], []
    cuts = sorted(r["bar_index"] for r in rows)
    cut = cuts[min(len(cuts) - 1, int(len(cuts) * frac))]
    train = [r for r in rows if r["bar_index"] <= cut - embargo]
    test = [r for r in rows if r["bar_index"] > cut]
    return train, test


def standardize(rows: list[dict], features: tuple = FEATURES
                ) -> tuple[list[list[float]], list[str], dict]:
    """Rows -> z-scored matrix. Returns (X, kept, stats).

    Columns with ~zero variance (e.g. ema_agree, which the replay holds at
    +1 by construction) are DROPPED and named — training on a constant is
    fitting the intercept twice, and silently keeping it would let a reader
    believe the model "uses" ten signals when it uses nine.
    """
    kept, stats = [], {}
    for f in features:
        vals = [float(r["features"][f]) for r in rows]
        n = len(vals)
        mean = sum(vals) / n if n else 0.0
        var = sum((v - mean) ** 2 for v in vals) / n if n else 0.0
        std = math.sqrt(var)
        if std < 1e-12:
            stats[f] = {"kept": False, "reason": "zero variance"}
            continue
        kept.append(f)
        stats[f] = {"kept": True, "mean": mean, "std": std}
    X = [[(float(r["features"][f]) - stats[f]["mean"]) / stats[f]["std"]
          for f in kept] for r in rows]
    return X, kept, stats


def apply_stats(rows: list[dict], kept: list[str], stats: dict
                ) -> list[list[float]]:
    """Standardize new rows with TRAIN stats. Never refit on test data —
    refitting would smuggle the test distribution into the model."""
    return [[(float(r["features"][f]) - stats[f]["mean"]) / stats[f]["std"]
             for f in kept] for r in rows]


def fit(X: list[list[float]], y: list[int], l2: float = 1.0,
        lr: float = 1.0, iters: int = 400) -> dict[str, Any]:
    """Full-batch gradient descent on L2 logistic loss. Deterministic:
    zero init, fixed order — retrains are bit-identical, no seed needed."""
    n = len(X)
    if not n or not X[0]:
        return {"weights": [], "bias": 0.0, "iters": 0}
    d = len(X[0])
    w = [0.0] * d
    b = 0.0
    for _ in range(max(0, iters)):
        gw = [0.0] * d
        gb = 0.0
        for xi, yi in zip(X, y):
            p = _sigmoid(sum(wi * xij for wi, xij in zip(w, xi)) + b)
            err = p - yi
            for j in range(d):
                gw[j] += err * xi[j]
            gb += err
        inv = 1.0 / n
        for j in range(d):
            w[j] -= lr * (gw[j] * inv + l2 * w[j] / n)
        b -= lr * gb * inv
    return {"weights": w, "bias": b, "iters": iters, "l2": l2, "lr": lr}


def predict_proba(X: list[list[float]], model: dict[str, Any]) -> list[float]:
    w, b = model.get("weights") or [], float(model.get("bias") or 0.0)
    return [_sigmoid(sum(wi * xij for wi, xij in zip(w, xi)) + b) for xi in X]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def evaluate(train_p: list[float], train_y: list[int], train_r: list[float],
             test_p: list[float], test_y: list[int], test_r: list[float],
             thresholds: tuple = (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70),
             min_taken: int = 100) -> dict[str, Any]:
    """Pick the threshold on TRAIN, report on TEST. Never the reverse.

    The business metric is taken-mean-R (money), not accuracy — a filter can
    be 60% "accurate" while losing money if it keeps the wrong 40%.
    """
    base_tr = _mean(train_r)
    base_te = _mean(test_r)
    rows = []
    for t in thresholds:
        tr = [r for p, r in zip(train_p, train_r) if p >= t]
        te = [r for p, r in zip(test_p, test_r) if p >= t]
        if len(tr) < min_taken:
            continue
        rows.append({
            "threshold": t, "train_n": len(tr),
            "train_mean_r": round(_mean(tr), 4),
            "test_n": len(te),
            "test_mean_r": round(_mean(te), 4) if te else 0.0,
            "test_precision": round(
                sum(1 for p, y in zip(test_p, test_y) if p >= t and y == 1)
                / max(1, len(te)), 4),
        })
    rows.sort(key=lambda r: -r["train_mean_r"])
    picked = rows[0] if rows else None
    return {
        "train_n": len(train_y), "test_n": len(test_y),
        "base_train_mean_r": round(base_tr, 4),
        "base_test_mean_r": round(base_te, 4),
        "base_test_precision": round(
            sum(test_y) / max(1, len(test_y)), 4),
        "thresholds": rows,
        "picked": picked,
        "holds_out": bool(picked and picked["test_mean_r"] > 0
                          and picked["test_mean_r"] > base_te),
    }
