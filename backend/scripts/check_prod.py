r"""Verify production (Render) is serving the gold feed after key setup.

Asks for the PIN interactively (getpass — never logged, never stored),
logs in, then checks /api/market/summary + /api/signals/latest for XAUUSD.

Run:  d:/tdapp/.venv/Scripts/python.exe scripts/check_prod.py
"""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys

import httpx

BASE = "https://tdapp-api.onrender.com"


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pin = (sys.argv[1] if len(sys.argv) > 1 else getpass.getpass("PIN (typing hidden): ")).strip()
    lines: list[str] = []

    async with httpx.AsyncClient(timeout=90.0) as c:
        login = await c.post(f"{BASE}/api/auth/login", json={"pin": pin})
        if login.status_code != 200 or not login.json().get("token"):
            lines.append(f"LOGIN FAILED ({login.status_code}) — wrong PIN? "
                         f"(failed_attempts increments; 5 = 15-min lock)")
            report = "\n".join(lines)
            print(report)
            return 1
        token = login.json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        lines.append("login: OK")

        mkt = (await c.get(f"{BASE}/api/market/summary", headers=hdr)).json()
        srcs = {}
        xau = None
        for row in mkt.get("assets", mkt if isinstance(mkt, list) else []):
            a = row.get("asset")
            srcs[a] = row.get("source", "?")
            if a == "XAUUSD":
                xau = row
        lines.append(f"market sources: {json.dumps(srcs, ensure_ascii=False)}")
        if xau:
            lines.append(f"XAUUSD: price={xau.get('close')} source={xau.get('source')} "
                         f"rsi={xau.get('rsi')}")
            lines.append("GOLD FEED: " + ("LIVE (Twelve Data)" if xau.get("source") == "live"
                                          else "FALLBACK (demo) — key not picked up"))
        else:
            lines.append("XAUUSD: MISSING from market/summary")

        sigs = (await c.get(f"{BASE}/api/signals/latest", headers=hdr)).json()
        if isinstance(sigs, list):
            lines.append(f"signals: {len(sigs)}")
            for s in sigs:
                lines.append(f"  {s.get('asset'):<7} entry={s.get('entry')} "
                             f"dir={s.get('direction')} conf={s.get('confidence')}")
        else:
            lines.append(f"signals: unexpected {type(sigs).__name__}")

    report = "\n".join(lines)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prod_check.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
