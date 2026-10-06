"""Train the meta-filter on a finished simulation run.

Run from backend/:
    C:/Python314/python.exe scripts/train_meta_filter.py
    C:/Python314/python.exe scripts/train_meta_filter.py --run <run_id>

WHAT IT DOES
------------
Loads the run's labelled events WITH entry-time features (migration 059;
featureless rows from older runs are skipped, never guessed), labels
y=1 iff the primary-cell trade ended profitable (r_multiple > 0), trains a
9-feature logistic regression on the early 70% (purged + embargo 20), picks
the take-threshold on TRAIN, and reports TEST-only numbers.

WHAT IT DOES NOT DO
-------------------
It does not touch live settings, the scanner, or the auto-trader. Its output
is a weights JSON under backend/artifacts/ plus a printed report. Wiring the
filter into production is a separate, explicit decision AFTER this report
holds out — a model file sitting on disk cannot trade by itself, which is
exactly the safety property required here.

Exit code is 0 even when the filter fails to hold out; a negative result is
still a result. The report says COLLAPSES plainly instead of hiding it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import meta_filter as mf

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


def load_events(db, run_id: str) -> tuple[list[dict], dict]:
    """Featured rows of one run, oldest-first. Paged; skips featureless rows.

    A row counts as trainable only when EVERY kept feature parses AND the
    label exists. Anything else is counted and skipped — a training set with
    silently-imputed values would teach the model our guesses, not the market.
    """
    cols = ["seq", "asset", "direction", "bar_index", "r_multiple",
            "mfe_r", "label"] + list(mf.FEATURES)
    rows: list[dict] = []
    skipped = {"no_features": 0, "no_label": 0}
    after = -1
    while True:
        page = db._client.table("simulation_events").select(",".join(cols)) \
            .eq("run_id", run_id).gt("seq", after).order("seq") \
            .limit(1000).execute().data or []
        if not page:
            break
        for r in page:
            try:
                rr = float(r.get("r_multiple"))
            except (TypeError, ValueError):
                skipped["no_label"] += 1
                continue
            feats = {}
            ok = True
            for f in mf.FEATURES:
                try:
                    v = float(r.get(f))
                    if v != v:
                        raise ValueError
                    feats[f] = v
                except (TypeError, ValueError):
                    ok = False
                    break
            if not ok:
                skipped["no_features"] += 1
                continue
            try:
                bar = int(r.get("bar_index") or 0)
            except (TypeError, ValueError):
                skipped["no_features"] += 1
                continue
            rows.append({"bar_index": bar, "features": feats,
                         "y": 1 if rr > 0 else 0, "r": rr,
                         "asset": r.get("asset"),
                         "seq": r.get("seq")})
        after = page[-1].get("seq", after)
        if len(page) < 1000:
            break
    info = {"rows": len(rows), "skipped": skipped}
    return rows, info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None,
                    help="run_id; default = latest finished run")
    ap.add_argument("--frac", type=float, default=0.7)
    ap.add_argument("--embargo", type=int, default=20)
    ap.add_argument("--min-taken", type=int, default=100)
    args = ap.parse_args()

    from app.services.database import Database
    db = Database()
    if not db or not db.available:
        print("DB unavailable — cannot train")
        return 1

    run_id = args.run
    if not run_id:
        runs = db.select("simulation_runs", order="created_at", desc=True,
                         limit=20) or []
        done = [r for r in runs if r.get("status") == "done"]
        if not done:
            print("no finished run to train on")
            return 1
        run_id = done[0]["id"]
        print(f"training on latest finished run {run_id[:8]} "
              f"({done[0].get('created_at')})")

    t0 = time.time()
    rows, info = load_events(db, run_id)
    print(f"trainable rows: {info['rows']}  skipped: {info['skipped']}")
    if len(rows) < 300:
        print("too few featured rows — run a simulation with the 059 "
              "columns first (older runs carry no features)")
        return 1

    train, test = mf.purged_split(rows, frac=args.frac, embargo=args.embargo)
    print(f"purged split: train {len(train)} / test {len(test)} "
          f"(embargo {args.embargo} bars)")
    if len(train) < 200 or len(test) < 60:
        print("split too small after purge — cannot evaluate honestly")
        return 1

    Xtr, kept, stats = mf.standardize(train)
    Xte = mf.apply_stats(test, kept, stats)
    dropped = [f for f in mf.FEATURES if f not in kept]
    if dropped:
        print(f"dropped zero-variance features: {dropped}")

    ytr = [r["y"] for r in train]
    yte = [r["y"] for r in test]
    rtr = [r["r"] for r in train]
    rte = [r["r"] for r in test]

    model = mf.fit(Xtr, ytr)
    ptr = mf.predict_proba(Xtr, model)
    pte = mf.predict_proba(Xte, model)
    out = mf.evaluate(ptr, ytr, rtr, pte, yte, rte,
                      min_taken=args.min_taken)

    print(f"\nbase (take all): train {out['base_train_mean_r']:+.3f}R  "
          f"test {out['base_test_mean_r']:+.3f}R  "
          f"test precision {out['base_test_precision']:.1%}")
    print(f"\n{'thr':>6}{'n_tr':>8}{'R_tr':>9}{'n_te':>8}{'R_te':>9}{'prec_te':>9}")
    for row in out["thresholds"]:
        mark = " <-- picked" if out["picked"] is row else ""
        print(f"{row['threshold']:>6.2f}{row['train_n']:>8}"
              f"{row['train_mean_r']:>+9.3f}{row['test_n']:>8}"
              f"{row['test_mean_r']:>+9.3f}{row['test_precision']:>9.1%}{mark}")

    pk = out["picked"]
    if pk:
        print(f"\nthreshold {pk['threshold']:.2f}: "
              f"train {pk['train_mean_r']:+.3f}R -> test {pk['test_mean_r']:+.3f}R")
    print("VERDICT: %s" % ("HOLDS OUT — worth a forward paper test"
                           if out["holds_out"]
                           else "COLLAPSES out-of-sample — do not wire in"))

    print("\nweights (standardized space):")
    for f, w in sorted(zip(kept, model["weights"]), key=lambda t: -abs(t[1])):
        print(f"  {f:16} {w:+.4f}")

    ARTIFACTS.mkdir(exist_ok=True)
    path = ARTIFACTS / f"meta-filter-{run_id[:8]}.json"
    path.write_text(json.dumps({
        "run_id": run_id, "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                      time.gmtime()),
        "features": kept, "stats": {f: stats[f] for f in kept},
        "model": model, "evaluation": out,
        "dropped_features": dropped,
    }, indent=1), encoding="utf-8")
    print(f"\nartifact: {path} ({path.stat().st_size} bytes)")
    print(f"total {time.time() - t0:.1f}s")
    print("\nNothing was changed in production. Wiring this in requires an "
          "explicit follow-up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
