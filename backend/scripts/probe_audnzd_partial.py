"""Probe prod: AUDNZD partial-close signal_logs + paper_trades state."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.services.database import Database  # noqa: E402


def main() -> int:
    db = Database()
    if not db.available:
        print("DB unavailable")
        return 1

    print("=== paper_trades AUDNZD ===")
    rows = db.select("paper_trades", filters={"asset": "AUDNZD"},
                     order="created_at", desc=True, limit=10)
    for r in rows:
        print(f"  {r.get('ticket')} status={r.get('status')} vol={r.get('volume')} "
              f"partial_done={r.get('partial_done')!r} pnl={r.get('pnl')}")

    print("=== signal_logs AUDNZD closed ===")
    logs = db.select("signal_logs", filters={"asset": "AUDNZD", "event": "closed"},
                     order="created_at", desc=True, limit=10)
    for l in logs:
        print(f"  {l.get('created_at')} ticket={l.get('ticket')} vol={l.get('volume')} "
              f"pnl={l.get('pnl')} reason={str(l.get('reason'))[:60]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
