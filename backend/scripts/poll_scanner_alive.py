"""Poll prod until the market scanner writes a NEW row (worker heartbeat check).

Usage: python poll_scanner_alive.py [max_minutes] [baseline_iso_prefix]
Writes nothing to the DB; read-only probe.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

BASE = "https://tdapp-api.onrender.com"
KNOWN_LATEST = "2026-09-03T16:40:41"  # last scan row seen (baseline arg overrides)


def req(path, token=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


tok = req("/api/auth/login", method="POST", body={"pin": "777777"})
token = tok.get("access_token") or tok.get("token")

max_min = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
if len(sys.argv) > 2:
    KNOWN_LATEST = sys.argv[2]
deadline = time.time() + max_min * 60

while time.time() < deadline:
    c = req("/api/system/counts", token=token)
    latest = str(c["market_analysis_latest"])
    print(f"{datetime.now(timezone.utc):%H:%M:%S} UTC  rows={c['market_analysis']}  latest={latest}")
    if not latest.startswith(KNOWN_LATEST):
        print("\nSCANNER_ALIVE — new scan row written after keepalive")
        print("signals latest:", c["signals_latest"])
        sys.exit(0)
    time.sleep(60)

print("\nTIMEOUT — no new scan row within", max_min, "minutes")
sys.exit(1)
