"""Production debug tool — verifies workers actually persist to the DB.

Checks (in order):
  1. GET /health        → workers must be "running" (scheduler started),
                          plus jobs list, db status, ai provider.
  2. GET /api/market/summary → regime + opportunities printout.
  3. GET /api/signals/latest twice, `--wait` seconds apart, compared
     PER-ENTRY (asset → price/confidence/direction).
       - all identical → STABLE  = rows served from DB (persistence works)
         (DEMO fallback is also stable, but /health db + explanation text
          disambiguate — printed below)
       - entries differ  → CHANGING = live-computed per request → workers
         are NOT persisting (the duplicate-lifespan bug signature)

Usage (from backend/):
  C:/Python314/python.exe scripts/check_live.py
  C:/Python314/python.exe scripts/check_live.py --wait 90
  C:/Python314/python.exe scripts/check_live.py https://other-host --wait 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx

DEFAULT_BASE = "https://tdapp-api.onrender.com"


def out(*parts: object) -> None:
    print(*parts, flush=True)


async def get_json(client: httpx.AsyncClient, path: str) -> object:
    r = await client.get(path, timeout=30.0)
    r.raise_for_status()
    return r.json()


def fingerprint(entries: list[dict]) -> dict[str, tuple]:
    """asset → (price, confidence, direction) — compare per entry, never the
    whole serialized array (the old PowerShell bug compared joined objects)."""
    fp: dict[str, tuple] = {}
    for e in entries:
        fp[e.get("asset", "?")] = (
            e.get("price"),
            e.get("confidence"),
            (e.get("direction") or "").upper(),
        )
    return fp


def diff_entries(a: dict[str, tuple], b: dict[str, tuple]) -> list[str]:
    changed = []
    for asset in sorted(set(a) | set(b)):
        if a.get(asset) != b.get(asset):
            changed.append(f"{asset}: {a.get(asset)} -> {b.get(asset)}")
    return changed


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default=DEFAULT_BASE)
    ap.add_argument("--wait", type=int, default=75,
                    help="seconds between the two signals snapshots (default 75)")
    ap.add_argument("--skip-wait", action="store_true",
                    help="run health+summary only, no stability probe")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    exit_code = 0
    async with httpx.AsyncClient() as client:
        # ---- 1. /health -------------------------------------------------
        out(f"== {base}/health")
        health = await get_json(client, "/health")
        out(json.dumps(health, ensure_ascii=False, indent=2))
        workers = health.get("workers")
        if workers == "running":
            out("✔ workers=running (scheduler started)",
                f"jobs={health.get('jobs')}")
        elif workers == "enabled":
            out("✘ workers=enabled but scheduler NOT running — the exact bug "
                "signature (env set, lifespan never started it). Deploy the "
                "fixed main.py and redeploy.")
            exit_code = 2
        else:
            out("⚠ workers=disabled (ENABLE_WORKERS not set — expected on "
                "web-only deployments)")
        out(f"  db={health.get('db')}  ai={health.get('ai')}")
        if health.get("db") != "ok":
            out("✘ db != ok — workers cannot persist at all (env/RLS/table)")
            exit_code = max(exit_code, 2)

        # ---- 2. /api/market/summary ------------------------------------
        out(f"\n== {base}/api/market/summary")
        summary = await get_json(client, "/api/market/summary")
        out(f"regime={summary.get('regime')} "
            f"confidence={summary.get('confidence')}")
        for o in summary.get("opportunities", []):
            out(f"  {o.get('asset'):8s} score={o.get('score'):>5} "
                f"price={o.get('price')}  | {str(o.get('reason', ''))[:80]}")

        if args.skip_wait:
            return exit_code

        # ---- 3. signals stability probe (per-entry) ---------------------
        out(f"\n== stability probe: /api/signals/latest x2 "
            f"(wait {args.wait}s)")
        snap1 = await get_json(client, "/api/signals/latest")
        fp1 = fingerprint(snap1)
        for asset, (price, conf, direction) in fp1.items():
            out(f"  t0 {asset:8s} {direction:4s} price={price} conf={conf}")
        out(f"  ... sleeping {args.wait}s ...")
        time.sleep(args.wait)
        snap2 = await get_json(client, "/api/signals/latest")
        fp2 = fingerprint(snap2)
        changed = diff_entries(fp1, fp2)
        if changed:
            out("✘ CHANGING → live-computed per request → workers are NOT "
                "persisting to DB:")
            for line in changed:
                out(f"    {line}")
            exit_code = max(exit_code, 3)
        else:
            out("✔ STABLE → rows served from persistent store (DB or DEMO).")
            out("  Disambiguate DEMO vs DB: DEMO explanations look like "
                "'demo/' placeholders; worker rows carry scanner text. "
                "With workers=running + db=ok above, stable = DB rows.")

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
