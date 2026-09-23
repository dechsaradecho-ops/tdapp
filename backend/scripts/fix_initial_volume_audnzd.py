"""Back-fix prod: set initial_volume on PAPER-000080 (AUDNZD partial case).

Migration 048's backfill deliberately skips rows with partial_done=true (the
guard was supposed to fill them from the order_opened signal-log). But
PAPER-000080 was already force-closed by fix_partial_done_audnzd.py, so the
guard will never manage it again -> initial_volume stays NULL and the history
row cannot show "closed / original".

This one-off reads the ORIGINAL size from the order_opened signal-log (never
guesses) and writes it. Idempotent: no-op when already set.

Run:  python scripts/fix_initial_volume_audnzd.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.services.database import Database  # noqa: E402

TICKET = "PAPER-000080"


def main() -> int:
    db = Database()

    rows = db.select("paper_trades", filters={"ticket": TICKET}, limit=1)
    if not rows:
        print(f"{TICKET}: not found")
        return 1
    row = rows[0]
    print("before:", {k: row.get(k) for k in
                      ("ticket", "volume", "initial_volume", "partial_done", "status")})
    if row.get("initial_volume") is not None:
        print("already set — nothing to do")
        return 0

    logs = db.select("signal_logs",
                     filters={"ticket": TICKET, "event": "order_opened"},
                     order="created_at", limit=1)
    if not logs or logs[0].get("volume") is None:
        print("no order_opened signal-log with a volume — refusing to guess")
        return 1
    original = float(logs[0]["volume"])
    print(f"order_opened volume = {original}")

    ok = db.update("paper_trades", str(row["id"]), {"initial_volume": original})
    print("update ok:", ok)

    after = db.select("paper_trades", filters={"ticket": TICKET}, limit=1)[0]
    print("after:", {k: after.get(k) for k in
                     ("ticket", "volume", "initial_volume", "partial_done", "status")})
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
