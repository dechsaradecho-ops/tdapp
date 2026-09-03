"""Live end-to-end check of the new quote feeds (Frankfurter + Twelve Data).

Usage:
  d:/tdapp/.venv/Scripts/python.exe scripts/check_quotes.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.integrations import quotes


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    assets = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"]

    snaps = await quotes.fetch_all_snapshots(assets)
    if not snaps:
        print("✘ no live snapshots at all")
        return 1
    for a in assets:
        s = snaps.get(a)
        if s is None:
            print(f"{a}: — (unavailable / fallback)")
            continue
        print(f"{a}: price={s['price']} rsi={s['rsi']} adx={s['adx']} "
              f"supertrend={s['supertrend_dir']} atr%={s['atr_pct']}")

    missing = [a for a in assets if a not in snaps]
    if missing == ["XAUUSD"]:
        print("\n⚠ XAUUSD unavailable — ตั้ง TWELVEDATA_API_KEY ใน .env "
              "(สมัครฟรีที่ twelvedata.com) แล้วรันใหม่")
    elif missing:
        print(f"\n⚠ missing: {missing}")
        return 1
    else:
        print("\n✔ all assets live")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
