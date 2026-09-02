"""Validate Supabase URL + secret key + worker tables BEFORE putting them in Render.

The key stays in YOUR local PowerShell session — never paste it into chat.

Usage (PowerShell):
  $env:SUPABASE_URL='https://<project-ref>.supabase.co'
  $env:SUPABASE_SECRET_KEY='sb_secret_...'
  C:/Python314/python.exe d:/tdapp/backend/scripts/check_supabase.py

Checks:
  1. REST root auth   → 401 means the key itself is rejected
  2. each worker table (select limit 1 + row count via content-range)
     → missing table = 002_worker_tables.sql / 001_initial_schema.sql not run
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

TABLES = ["market_analysis", "signals", "news_analysis", "trades", "portfolios"]


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        print("Set $env:SUPABASE_URL and $env:SUPABASE_SECRET_KEY first.")
        return 1

    print(f"url = {url}")
    print(f"key = {key[:12]}...{key[-4:]}  (len={len(key)})")
    if key.startswith("sb_publishable_"):
        print("✘ That is a PUBLISHABLE key (frontend only). Backend needs sb_secret_...")
        return 2
    if key.startswith("eyJ"):
        print("⚠ Legacy JWT key detected — service_role works, but prefer sb_secret_...")

    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    exit_code = 0
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{url}/rest/v1/", headers=headers, timeout=15)
        print(f"\nREST root: HTTP {r.status_code}")
        if r.status_code in (401, 403):
            print("✘ INVALID API KEY — Supabase rejected it. Re-copy the sb_secret_ key")
            print("  from Supabase Dashboard → Project Settings → API Keys → Secret keys.")
            return 2

        print("\nTables:")
        for t in TABLES:
            try:
                r = await c.get(f"{url}/rest/v1/{t}", headers=headers,
                                params={"select": "*", "limit": 1}, timeout=15)
            except httpx.HTTPError as exc:
                print(f"✘ {t}: network error {exc}")
                exit_code = max(exit_code, 3)
                continue
            if r.status_code == 200:
                print(f"✔ {t}: ok ({r.headers.get('content-range', 'count=?')})")
            elif r.status_code == 404:
                print(f"✘ {t}: MISSING — run database/00x_*.sql in Supabase SQL Editor")
                exit_code = max(exit_code, 3)
            else:
                print(f"✘ {t}: HTTP {r.status_code} {r.text[:140]}")
                exit_code = max(exit_code, 3)

    if exit_code == 0:
        print("\n✔ All good — paste EXACTLY these values into Render env "
              "(no trailing spaces/newlines), Save, wait for redeploy.")
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
