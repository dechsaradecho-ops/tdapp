"""Probe prod: is migration 048 (paper_trades.initial_volume) applied?

Read-only. Prints:
  * whether the column exists (a select of it succeeds),
  * how many rows have it set vs NULL,
  * the AUDNZD PAPER-000080 row (the partial-close case) in detail,
  * a few recent partial-closed rows so we can eyeball closed/original.

Run:  python scripts/probe_initial_volume.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.services.database import Database  # noqa: E402


def main() -> int:
    db = Database()
    print("db client:", type(db._client).__name__)

    # 1) Does the column exist? A raw select naming it fails loudly if not.
    try:
        resp = (db._client.table("paper_trades")
                .select("id,ticket,volume,initial_volume,partial_done,status")
                .order("created_at", desc=True).limit(5).execute())
        rows = list(resp.data or [])
        print("COLUMN initial_volume: EXISTS")
        print("sample rows:")
        for r in rows:
            print("  ", {k: r.get(k) for k in
                         ("ticket", "volume", "initial_volume", "partial_done", "status")})
    except Exception as exc:
        print("COLUMN initial_volume: MISSING ->", exc)
        return 1

    # 2) Coverage: how many rows have it set?
    try:
        all_rows = db.select_paged("paper_trades", order="created_at")
        total = len(all_rows)
        have = sum(1 for r in all_rows if r.get("initial_volume") is not None)
        null_partial = [r for r in all_rows
                        if r.get("initial_volume") is None
                        and r.get("partial_done")]
        print(f"rows total={total} with initial_volume={have} "
              f"null-but-partial_done={len(null_partial)}")
        for r in null_partial[:10]:
            print("   NULL+partial:", {k: r.get(k) for k in
                                       ("ticket", "volume", "partial_done", "status")})
    except Exception as exc:
        print("coverage probe failed:", exc)

    # 3) The known partial-close case.
    try:
        hit = db.select("paper_trades", filters={"ticket": "PAPER-000080"}, limit=1)
        print("PAPER-000080:", {k: hit[0].get(k) for k in
                                ("ticket", "volume", "initial_volume",
                                 "partial_done", "status")} if hit else "not found")
    except Exception as exc:
        print("PAPER-000080 probe failed:", exc)

    # 4) Recent scaled-out rows (partial_done true) to eyeball closed/original.
    try:
        scaled = [r for r in all_rows if r.get("partial_done")]
        scaled.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        print(f"scaled-out rows: {len(scaled)}")
        for r in scaled[:10]:
            vol = r.get("volume")
            iv = r.get("initial_volume")
            closed = (float(iv) - float(vol)) if (iv is not None and vol is not None) else None
            print(f"   {r.get('ticket')}: closed={closed} / original={iv} "
                  f"(remaining={vol}, status={r.get('status')})")
    except Exception as exc:
        print("scaled-out probe failed:", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
