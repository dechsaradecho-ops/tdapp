"""Count rows per worker table in Supabase — needs SUPABASE_URL + SUPABASE_SECRET_KEY env.

Usage (PowerShell):
  $env:SUPABASE_URL='https://<ref>.supabase.co'; $env:SUPABASE_SECRET_KEY='sb_secret_...'
  C:/Python314/python.exe d:/tdapp/backend/scripts/count_rows.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

TABLES = ["market_analysis", "signals", "news_analysis", "trades", "notifications"]


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        print("Set $env:SUPABASE_URL and $env:SUPABASE_SECRET_KEY first.")
        return 1
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Prefer": "count=exact"}
    exit_code = 0
    async with httpx.AsyncClient() as c:
        for t in TABLES:
            r = await c.get(f"{url}/rest/v1/{t}", headers=headers,
                            params={"select": "created_at", "limit": 1}, timeout=15)
            rng = r.headers.get("content-range", "?")
            print(f"{t:18s} HTTP {r.status_code}  count: {rng}")
            if r.status_code != 200:
                exit_code = 3
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
