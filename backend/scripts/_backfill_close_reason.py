"""Back-fill close_reason / pnl / exit_price on paper_trades rows that were
closed without a reason (force-closed by a script, or closed in slices until
volume hit 0 before close_trade_rows wrote the reason).

Prod 2026-09-23: PAPER-000080 (AUDNZD) had status=closed, volume=0.0,
close_reason=None, pnl=None, exit_price=None — the monitor's "เหตุผลปิด"
column showed nothing for a trade that really did close.

Source of truth = signal_logs (event=closed) for that ticket: sum the slice
PnL, take the last exit price, and derive a reason code from the prose.

Run:  python scripts/_backfill_close_reason.py            (dry run)
      python scripts/_backfill_close_reason.py --apply    (write)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.services.database import Database  # noqa: E402

APPLY = "--apply" in sys.argv


def reason_code(prose: str) -> str:
    p = (prose or "").lower()
    if "tp1" in p or "ปิดบางส่วน" in prose:
        return "tp"
    if "smart_exit" in p:
        return "smart_exit"
    if "sl" in p:
        return "sl"
    return "manual"


def main() -> None:
    db = Database()
    # Rows that are closed but carry no reason.
    rows = db.select("paper_trades", filters={"status": "closed"}, limit=500)
    targets = [r for r in rows if not r.get("close_reason")]
    print(f"closed rows without close_reason: {len(targets)}")
    for r in targets:
        ticket = str(r.get("ticket") or "")
        logs = db.select("signal_logs", filters={"ticket": ticket},
                         order="created_at", limit=200)
        closed = [l for l in logs if l.get("event") == "closed"]
        if not closed:
            print(f"  {ticket} {r.get('asset')}: no closed log — skip")
            continue
        pnl = sum(float(l.get("pnl") or 0.0) for l in closed)
        exit_price = None
        for l in reversed(closed):
            if l.get("exit_price") is not None:
                exit_price = float(l["exit_price"])
                break
        code = reason_code(str(closed[-1].get("reason") or ""))
        patch = {"close_reason": code}
        if pnl:
            patch["pnl"] = round(pnl, 2)
        if exit_price is not None:
            patch["exit_price"] = exit_price
        print(f"  {ticket} {r.get('asset')}: -> {patch} "
              f"(from {len(closed)} closed logs)")
        if APPLY:
            db.update("paper_trades", r["id"], patch)
    print("APPLIED" if APPLY else "DRY RUN — pass --apply to write")


if __name__ == "__main__":
    main()
