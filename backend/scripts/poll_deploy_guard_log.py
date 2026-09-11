r"""Poll prod until position_guard writes scheduler_runs rows (fix cc699b2).

Before the fix the guard cycle took ~55s, so with max_instances=1 APScheduler
skipped every following tick and position_guard NEVER logged -> the Logs page
wrongly blamed migration 030. After the fix a cycle costs ~max(caps) instead
of the sum, plus a 50s watchdog always writes a row.

This script only READS (scheduler-logs). It does not touch the paper book.

Run:  d:/tdapp/.venv/Scripts/python.exe scripts/poll_deploy_guard_log.py 777777
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx

BASE = "https://tdapp-api.onrender.com"
DEADLINE_S = 900.0     # 15 min: Render build ~2-4 min + first guard tick
EVERY_S = 15.0


async def snapshot(c: httpx.AsyncClient, hdr: dict) -> tuple[int, dict, list]:
    r = await c.get(f"{BASE}/api/system/scheduler-logs",
                    params={"job": "position_guard", "limit": 5}, headers=hdr)
    if r.status_code == 401:
        raise _Reauth()
    r.raise_for_status()
    d = r.json()
    return int(d.get("total") or 0), d.get("summary") or {}, d.get("logs") or []


class _Reauth(Exception):
    """Prod restarted (deploy) -> the in-memory session token was wiped."""


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pin = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not pin:
        print("usage: poll_deploy_guard_log.py <PIN>")
        return 2

    async with httpx.AsyncClient(timeout=60.0) as c:
        async def login_now() -> dict:
            r = await c.post(f"{BASE}/api/auth/login", json={"pin": pin})
            if r.status_code != 200 or not r.json().get("token"):
                raise RuntimeError(f"login FAILED ({r.status_code})")
            return {"Authorization": f"Bearer {r.json()['token']}"}

        hdr = await login_now()

        started = time.monotonic()
        last_note = ""
        while time.monotonic() - started < DEADLINE_S:
            elapsed = time.monotonic() - started
            try:
                total, summary, logs = await snapshot(c, hdr)
            except _Reauth:
                # Deploy restart wiped the session -> re-auth and keep polling.
                print(f"[{elapsed:6.0f}s] 401 (API restarted by deploy) -- re-login")
                hdr = await login_now()
                await asyncio.sleep(EVERY_S)
                continue
            except Exception as exc:                      # deploy restart window
                print(f"[{elapsed:6.0f}s] not ready: {type(exc).__name__}: {exc}")
                await asyncio.sleep(EVERY_S)
                continue

            by_job = summary.get("by_job") or {}
            guard = by_job.get("position_guard", 0)
            note = (f"[{elapsed:6.0f}s] guard rows={total} summary.total="
                    f"{summary.get('total')} by_job.position_guard={guard}")
            if note != last_note:
                print(note)
                last_note = note

            if total > 0 or guard > 0:
                durs = [r.get("duration_ms") for r in logs if r.get("duration_ms")]
                worst = max(durs) if durs else None
                print("\n=== GUARD NOW LOGS ===")
                print(f"rows={total} worst_duration_ms={worst}")
                for r in logs[:5]:
                    print(f"  {r.get('created_at')} status={r.get('status')} "
                          f"duration_ms={r.get('duration_ms')} "
                          f"detail={json.dumps(r.get('detail'), ensure_ascii=False)[:160]}")
                if worst is not None and worst >= 60_000:
                    print("\nVERDICT: still >=60s per cycle -- not fixed?")
                    return 1
                print("\nVERDICT: position_guard is logging (fix cc699b2 live)")
                return 0

            await asyncio.sleep(EVERY_S)

        print("\nTIMEOUT: no position_guard rows within 15 min")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
