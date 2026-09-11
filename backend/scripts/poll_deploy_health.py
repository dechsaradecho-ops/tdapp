r"""Poll /health until the deployed commit matches local HEAD, then report
scheduler forensics (job_next / job_running) and guard log rows.

Why: `git push` succeeding says nothing about whether Render actually
rebuilt. Render injects RENDER_GIT_COMMIT into /health as `.commit`, so this
script can prove WHICH commit prod is running — and if autoDeploy is off it
will sit there telling you so instead of you guessing.

Run:  d:/tdapp/.venv/Scripts/python.exe backend/scripts/poll_deploy_health.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import httpx

BASE = "https://tdapp-api.onrender.com"
DEADLINE_S = 1200.0    # 20 min — starter plan builds are slow
EVERY_S = 20.0


def local_sha() -> str:
    out = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"],
                         capture_output=True, text=True, check=False)
    return (out.stdout or "").strip()


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    want = local_sha()
    print(f"local HEAD = {want}")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as c:
        while time.monotonic() - started < DEADLINE_S:
            el = time.monotonic() - started
            try:
                h = (await c.get(f"{BASE}/health")).json()
            except Exception as exc:
                print(f"[{el:6.0f}s] health unreachable: {type(exc).__name__}")
                await asyncio.sleep(EVERY_S)
                continue
            got = h.get("commit")
            print(f"[{el:6.0f}s] live commit = {got}  workers={h.get('workers')} "
                  f"running={json.dumps(h.get('job_running'))}")
            if got == want:
                print("\n=== DEPLOY LIVE ===")
                print("job_next:", json.dumps(h.get("job_next"), indent=2))
                tok = (await c.post(f"{BASE}/api/auth/login",
                                    json={"pin": "777777"})).json().get("token")
                if not tok:
                    print("login failed — rerun with a PIN (auth is in-memory)")
                    return 1
                d = (await c.get(f"{BASE}/api/system/scheduler-logs",
                                 params={"job": "position_guard", "limit": 5},
                                 headers={"Authorization": f"Bearer {tok}"})).json()
                print(f"\nguard rows = {d.get('total')}")
                print("by_job:", json.dumps(d["summary"]["by_job"]))
                for r in d.get("logs", [])[:5]:
                    print(" ", r["created_at"], r["status"],
                          r.get("duration_ms"), repr(r.get("detail"))[:140],
                          repr(r.get("error"))[:140])
                return 0
            await asyncio.sleep(EVERY_S)
    print(f"\nTIMEOUT after {DEADLINE_S/60:.0f} min — prod never moved to {want}."
          "\nautoDeploy may be OFF for tdapp-api: trigger it from the Render"
          " Dashboard (Manual Deploy → Deploy latest commit), then rerun.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
