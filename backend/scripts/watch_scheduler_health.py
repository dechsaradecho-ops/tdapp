r"""Watch /health and name the culprit when a scheduled job stops logging.

`job_running` (executor `_instances`) tells us which jobs currently hold a
max_instances=1 slot. A job that stays in that dict across several samples is
NOT slow — it is STUCK: its tick never returns, so every later tick is
skipped and no scheduler_runs row can ever be written (matching an empty
job history with zero error rows).

`job_next` drift is the second signal: APScheduler moves next_run_time each
time a tick fires, so a job whose next_run_time advances while never logging
is firing-and-vanishing rather than never being scheduled.

Read-only: hits /health only, no auth, no state touched.

Run:  d:/tdapp/.venv/Scripts/python.exe backend/scripts/watch_scheduler_health.py [seconds]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx

BASE = "https://tdapp-api.onrender.com"
EVERY_S = 5.0


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    total_s = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    seen_running: dict[str, int] = {}      # job -> consecutive samples running
    last_next: dict[str, str] = {}
    stuck: set[str] = set()

    async with httpx.AsyncClient(timeout=30.0) as c:
        started = time.monotonic()
        while time.monotonic() - started < total_s:
            el = time.monotonic() - started
            try:
                h = (await c.get(f"{BASE}/health")).json()
            except Exception as exc:
                print(f"[{el:6.0f}s] health error: {type(exc).__name__}")
                await asyncio.sleep(EVERY_S)
                continue

            running = h.get("job_running") or {}
            nxt = h.get("job_next") or {}

            # jobs that advanced their next fire time since the last sample
            fired = sorted(j for j, t in nxt.items()
                           if j in last_next and last_next[j] != t)
            last_next.update(nxt)

            for job in list(seen_running):
                if job not in running:
                    seen_running.pop(job, None)

            flagged = []
            for job in running:
                seen_running[job] = seen_running.get(job, 0) + 1
                if seen_running[job] >= 3:      # ~15s of a 1-min slot = suspicious
                    stuck.add(job)
                    flagged.append(job)

            print(f"[{el:6.0f}s] commit={h.get('commit')} running={json.dumps(running)}"
                  f" fired={','.join(fired) or '-'}"
                  + (f"  ⚠ STUCK>{seen_running[job]*EVERY_S:.0f}s={flagged}" if flagged else ""))
            await asyncio.sleep(EVERY_S)

    print("\n=== VERDICT ===")
    if stuck:
        print(f"STUCK JOBS: {sorted(stuck)}")
        print("These hold a max_instances=1 slot without returning, so every")
        print("later tick is skipped and NO scheduler_runs row can be written.")
        print("=> the job body blocks the event loop / never returns (a")
        print("   watchdog inside the same loop cannot fire either).")
        return 0
    print("No job stayed running across >=3 samples — nothing is stuck.")
    print("If a job still logs nothing, it is not being scheduled at all")
    print("(check job_next: a frozen/missing next_run_time) or its log write")
    print("is failing silently.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
