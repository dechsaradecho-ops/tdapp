r"""Compare /api/signals/latest entry prices against a FRESH live quote fetch.

Answers the user question: "ราคา signal อัพเดทแล้วหรือยัง?"
For each signal asset: live snapshot is fetched directly from quotes.py
(same code path the scanner uses), then compared to the signal entry.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.integrations.quotes import fetch_snapshot  # noqa: E402

BASE = "http://127.0.0.1:8123"


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    lines: list[str] = []

    async with httpx.AsyncClient(timeout=90.0) as c:
        st = (await c.get(f"{BASE}/api/auth/status")).json()
        token = None
        if st.get("pin_set"):
            r = await c.post(f"{BASE}/api/auth/login", json={"pin": "123456"})
            if r.status_code != 200:
                r = await c.post(f"{BASE}/api/auth/set-pin", json={"pin": "123456"})
            token = r.json().get("token")
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        sigs = (await c.get(f"{BASE}/api/signals/latest", headers=headers)).json()
        lines.append(f"signals: {len(sigs) if isinstance(sigs, list) else type(sigs).__name__}")

        if not isinstance(sigs, list) or not sigs:
            lines.append("NO SIGNALS — DB unavailable and/or feed down")
        else:
            for s in sigs:
                asset = s.get("asset", "?")
                entry = s.get("entry")
                created = s.get("created_at") or s.get("timestamp") or s.get("generated_at") or "?"
                src = s.get("source", "?")
                try:
                    snap = await fetch_snapshot(asset, c)
                    live: float | None = snap.get("close") or snap.get("price")
                    live_err = ""
                except Exception as exc:
                    live = None
                    live_err = f"{exc.__class__.__name__}: {exc}"
                if live is None:
                    lines.append(f"{asset:<7} entry={entry} created={created} src={src} "
                                 f"LIVE=ERROR ({live_err})")
                else:
                    diff = (live - entry) if isinstance(entry, (int, float)) else None
                    pct = (diff / entry * 100) if diff is not None and entry else None
                    lines.append(
                        f"{asset:<7} entry={entry} live={live:.5f} "
                        f"diff={diff:+.5f} ({pct:+.2f}%) created={created} src={src}")

    report = "\n".join(lines)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_price_check.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
