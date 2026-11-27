"""Run rolling walk-forward refits for a frozen optimizer baseline.

Usage:
  python backend/scripts/run_walk_forward.py \
    --dataset-version v20261126-120951-1d5254 \
    --baseline-run opt-20261127-062606-494f21 \
    --output C:/temp/opt-walk-forward.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

BACKEND_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, BACKEND_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BACKEND_ROOT, ".env"))
except Exception:
    pass

from app.models.schemas import AppSettings
from app.services.database import Database
from app.services.sim_walk_forward import run_walk_forward


DEFAULT_ASSETS = (
    "EURUSD,USDJPY,AUDUSD,XAUUSD,GBPCHF,EURCHF,AUDNZD,AUDCHF,"
    "CADCHF,GBPJPY,CHFJPY,CADJPY,EURGBP,USDCAD,AUDCAD,EURCAD"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-version", default="v20261126-120951-1d5254")
    parser.add_argument(
        "--baseline-run", default="opt-20261127-062606-494f21")
    parser.add_argument("--assets", default=DEFAULT_ASSETS)
    parser.add_argument("--output", default="")
    parser.add_argument("--fold-count", type=int, default=4)
    parser.add_argument("--train-days", type=int, default=365)
    parser.add_argument("--test-days", type=int, default=90)
    parser.add_argument("--warmup-days", type=int, default=200)
    parser.add_argument("--max-iterations", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed-start", type=int, default=100)
    parser.add_argument("--starting-capital", type=float, default=1000.0)
    parser.add_argument("--commission", type=float, default=3.5)
    parser.add_argument("--exit-spread-mult", type=float, default=0.5)
    return parser.parse_args()


def load_baseline(db: Database, run_id: str) -> AppSettings:
    rows = db.select(
        "sim_opt_runs", filters={"id": run_id},
        order="created_at", desc=True, limit=1,
    )
    if not rows:
        raise ValueError(f"baseline run not found: {run_id}")
    result = rows[0].get("result") or {}
    recommendation = result.get("recommendation") or {}
    config = recommendation.get("config")
    if not isinstance(config, dict):
        raise ValueError(f"baseline run has no recommendation config: {run_id}")
    return AppSettings.model_validate(config)


def main() -> int:
    args = parse_args()
    db = Database()
    if not db.available:
        print(f"DB unavailable: {db.init_error}", file=sys.stderr)
        return 1
    try:
        baseline = load_baseline(db, args.baseline_run)
        assets = [
            item.strip().upper()
            for item in args.assets.split(",")
            if item.strip()
        ]
        report = run_walk_forward(
            db,
            args.dataset_version,
            assets,
            baseline,
            fold_count=args.fold_count,
            train_days=args.train_days,
            test_days=args.test_days,
            warmup_days=args.warmup_days,
            max_iterations=args.max_iterations,
            workers=args.workers,
            seed_start=args.seed_start,
            starting_capital=args.starting_capital,
            commission_per_lot_per_side=args.commission,
            exit_spread_mult=args.exit_spread_mult,
        )
        text = json.dumps(report, indent=2, ensure_ascii=False)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
        else:
            print(text)
        return 0
    except Exception as exc:
        print(f"walk-forward failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
