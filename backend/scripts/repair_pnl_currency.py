r"""One-off: back-fix historical paper_trades.pnl stored in the QUOTE currency.

WHY: before commit 229d855 the close path stored ``sign × (exit − entry) ×
lots × contract`` verbatim — that value is in the QUOTE currency, not USD.
USDJPY 0.02 lots over 157.024→157.013 stored 22.0 (¥22 read as $22); every
JPY- and NZD-quote pair is likewise inflated ~150× / ~1.75×.

This script recomputes each closed row's PnL the SAME way the live code now
does (``PaperBrokerPnl.compute`` with ``fetch_pnl_rates``) using the stored
entry/exit prices, and rewrites ``pnl`` only when it differs. USD-quote pairs
(EURUSD/GBPUSD/XAUUSD/NZDUSD) get rate 1.0 and are left untouched.

Dry-run by default; pass ``--apply`` to write. Never raises on a single row.

Run:
    d:/tdapp/.venv/Scripts/python.exe scripts/repair_pnl_currency.py          # dry
    d:/tdapp/.venv/Scripts/python.exe scripts/repair_pnl_currency.py --apply  # write
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.database import Database              # noqa: E402
from app.services import execution                      # noqa: E402


def _reconfigure() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


async def main(apply: bool) -> int:
    _reconfigure()
    db = Database()
    if not db or not db.available:
        print("DB unavailable — abort")
        return 3

    rows = db.select("paper_trades", filters={"status": "closed"},
                     order="created_at", desc=True, limit=500)
    if not rows:
        print("no closed rows")
        return 0

    # one trusted spot map for every conversion leg needed
    assets = sorted({str(r.get("asset") or "").upper() for r in rows if r.get("asset")})
    seed = {}
    for r in rows:
        a = str(r.get("asset") or "").upper()
        ex = r.get("exit_price")
        if a and ex and float(ex or 0) > 0:
            seed.setdefault(a, float(ex))
    rates = await execution.fetch_pnl_rates(assets, seed=seed)
    print(f"rates fetched for {len(rates)} legs: {sorted(rates.keys())}\n")

    changed = 0
    skipped = 0
    for r in rows:
        asset = str(r.get("asset") or "").upper()
        stored = r.get("pnl")
        entry = r.get("entry_price")
        exit_p = r.get("exit_price")
        if stored is None or not entry or not exit_p:
            skipped += 1
            continue
        pos = SimpleNamespace(
            direction=str(r.get("direction") or "").upper(),
            current_price=float(exit_p),
            entry_price=float(entry),
            volume=float(r.get("volume") or 0),
            asset=asset,
        )
        fixed = execution.PaperBrokerPnl.compute(pos, asset=asset, rates=rates)
        if fixed is None:
            print(f"  SKIP {asset} {r.get('id')} — no conversion rate "
                  f"(quote={execution.quote_currency(asset)})")
            skipped += 1
            continue
        fixed = round(fixed, 2)
        if abs(fixed - float(stored)) < 0.005:
            continue
        print(f"  FIX {asset:7s} {str(r.get('id'))[:8]}  "
              f"{float(stored):+8.2f} -> {fixed:+8.2f}  "
              f"(quote {execution.quote_currency(asset)})")
        changed += 1
        if apply:
            db.update("paper_trades", str(r.get("id")), {"pnl": fixed})

    print(f"\n{'APPLIED' if apply else 'DRY-RUN'}: {changed} would change, "
          f"{skipped} skipped, {len(rows)} scanned")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
