"""One-off: back-fix the AUDNZD PAPER-000080 partial-close state on prod.

Why: before migration 047 the guard's `partial_done` write was silently
rejected (unknown column), so the row still read partial_done=None and its
`volume` was never reduced. The position partial-closed 4x across worker
restarts (01:05 / 03:00 / 03:15 / 03:30), each restart rehydrating volume
back to 0.02, so the stored size drifted to 0.0 while the row stayed `open`.

The true state: the whole 0.02 was scaled out, so the position is CLOSED
with volume 0.0. This script:
  1. prints the current row (audit trail),
  2. sets status=closed, partial_done=True, volume=0.0,
  3. re-reads and prints the result.

Run AFTER migration 047 is applied in the Supabase SQL Editor.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.services.database import Database  # noqa: E402

TICKET = "PAPER-000080"
REMAINING_VOLUME = 0.0


def main() -> int:
    db = Database()
    if not db.available:
        print("DB unavailable — check .env")
        return 1

    rows = db.select("paper_trades", filters={"ticket": TICKET}, limit=5)
    if not rows:
        print(f"no row for ticket={TICKET}")
        return 1

    row = rows[0]
    print("BEFORE:")
    for k in ("id", "ticket", "asset", "status", "volume",
              "partial_done", "entry_price", "stop_loss", "take_profit"):
        print(f"  {k} = {row.get(k)!r}")

    if (row.get("status") == "closed"
            and row.get("partial_done") is True
            and float(row.get("volume") or 0) == REMAINING_VOLUME):
        print("already fixed — nothing to do")
        return 0

    db.update("paper_trades", str(row["id"]),
              {"status": "closed", "partial_done": True,
               "volume": REMAINING_VOLUME})

    after = db.select("paper_trades", filters={"ticket": TICKET}, limit=1)
    print("AFTER:")
    if after:
        for k in ("id", "ticket", "status", "volume", "partial_done"):
            print(f"  {k} = {after[0].get(k)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
