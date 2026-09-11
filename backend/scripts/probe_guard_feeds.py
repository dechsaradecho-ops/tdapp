r"""Read-only probe: time the position_guard feed phase exactly as it runs.

Reproduces the three independent fetches guard_once performs each minute
(live spot marks / Smart-Exit snapshots / news calendar) BOTH sequentially
(old code) and concurrently (new code), and reports per-feed wall time plus
any BaseException (CancelledError is a BaseException and would slip past
`except Exception`, leaving scheduler_runs empty with status=ok never written).

Touches NO trading state: no order, no SL/TP, no broker book.

Run from backend/:  python scripts/probe_guard_feeds.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import traceback

sys.path.insert(0, ".")

from app.core.config import get_settings          # noqa: E402
from app.services.database import Database        # noqa: E402
from app.integrations import quotes               # noqa: E402
from app.services import execution                # noqa: E402
from app.workers import position_guard as pg      # noqa: E402


async def timed(name: str, coro) -> float:
    t0 = time.monotonic()
    try:
        r = await coro
        dt = time.monotonic() - t0
        keys = len(r) if isinstance(r, dict) else r
        print(f"  {name:<28} ok    {dt:6.2f}s  -> {keys}")
        return dt
    except BaseException as exc:                   # noqa: BLE001
        dt = time.monotonic() - t0
        print(f"  {name:<28} RAISE {dt:6.2f}s  {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)
        return dt


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    db = Database()
    if not db.available:
        print("DB unavailable:", db.init_error)
        return 1
    s = execution.get_app_settings(db)

    rows = db.select("paper_trades", filters={"status": "open"}, limit=50)
    assets = sorted({str(r.get("asset") or "").upper() for r in rows if r.get("asset")})
    print(f"open paper_trades={len(rows)} assets={assets}")
    if not assets:
        assets = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD", "USDCAD", "NZDUSD"]
        print("(no open rows — probing a representative asset basket)")

    print("\nSEQUENTIAL (old code path):")
    seq = 0.0
    seq += await timed("_live_marks (spot)", pg._live_marks(assets))
    seq += await timed("fetch_all_snapshots", quotes.fetch_all_snapshots(assets))
    seq += await timed("_smart_exit_news", pg._smart_exit_news(db, s))
    print(f"  TOTAL sequential = {seq:.2f}s   (1-min interval ⇒ "
          f"{'OVER' if seq > 60 else 'under'} budget)")

    print("\nCONCURRENT (new code path, per-branch caps):")
    t0 = time.monotonic()
    marks_c, snaps_c, news_c = await asyncio.gather(
        pg._bounded(pg._live_marks(assets), pg._GUARD_MARKS_BUDGET),
        (pg._bounded(quotes.fetch_all_snapshots(assets), pg._GUARD_SNAP_BUDGET)
         if getattr(s, "smart_exit_enabled", True) else pg._none()),
        (pg._bounded(pg._smart_exit_news(db, s), pg._GUARD_NEWS_BUDGET)
         if getattr(s, "smart_exit_enabled", True) else pg._none()),
    )
    wall = time.monotonic() - t0
    print(f"  marks={len(marks_c or {})} snaps={len(snaps_c or {})} news={news_c}")
    print(f"  TOTAL concurrent = {wall:.2f}s   (worst case = max cap = "
          f"{max(pg._GUARD_MARKS_BUDGET, pg._GUARD_SNAP_BUDGET, pg._GUARD_NEWS_BUDGET):.0f}s)")

    print(f"\nVERDICT: sequential {seq:.1f}s vs concurrent {wall:.1f}s "
          f"({seq - wall:+.1f}s saved per cycle)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
