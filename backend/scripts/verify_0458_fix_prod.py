"""Verify the fail-closed duplicate gate deploy is live in prod.

Evidence the fix shipped (commit 8ebb98d):
  1. /api/trading/monitor — the duplicate PAPER-000016 (XAUUSD) and
     PAPER-000017 (AUDUSD) orders must now be CLOSED (position_guard /
     manual close), not sitting open on top of the originals.
  2. Signal log — recent events must show the new fail-safe wording
     ("อ่านสถานะไม้เปิดไม่สำเร็จ") if a DB hiccup occurred since deploy,
     and NO duplicate order_opened pairs.

Writes UTF-8 report; Thai-safe (no Thai on stdout).
"""
import json
import time
import urllib.request

BASE = "https://tdapp-api.onrender.com"
PIN = "777777"
OUT = __file__.replace("verify_0458_fix_prod.py", "_verify_0458_fix.txt")


def req(path, token=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


# 1) wait for the Render deploy to come back up (restart drops /health)
print("waiting for deploy (health checks every 20s, up to ~7 min)...")
code, text = 0, ""
for attempt in range(21):
    code, text = req("/health")
    if code == 200:
        break
    print(f"health {code}, retry in 20s ({attempt + 1}/21)")
    time.sleep(20)
print(f"health: {code}")

# 2) login
code, text = req("/api/auth/login", method="POST", body={"pin": PIN})
assert code == 200, f"login failed: {code} {text}"
token = json.loads(text).get("access_token") or json.loads(text).get("token")

lines = [f"health {code} @ {time.strftime('%H:%M:%S')}"]

# 3) monitor snapshot — state of the once-duplicated positions
code, text = req("/api/trading/monitor", token=token)
snap = json.loads(text)
lines.append(f"monitor HTTP {code} as_of={snap.get('as_of')}")
lines.append(f"stats: {json.dumps(snap.get('stats', {}), ensure_ascii=False)}")
lines.append("--- open positions ---")
for p in snap.get("open_positions", []):
    lines.append(f"{p.get('asset')} ticket={p.get('ticket')} "
                 f"entry={p.get('entry_price')} current={p.get('current_price')} "
                 f"uPnL={p.get('unrealized_pnl')}")

# 4) signal log — look for fail-safe wording + any new duplicate opens
code, text = req("/api/trading/signals/logs?limit=60", token=token)
if code != 200:  # correct endpoint is /api/system/signal-logs
    code, text = req("/api/system/signal-logs?limit=60", token=token)
logs = json.loads(text)
rows = logs.get("logs") if isinstance(logs, dict) else logs
if not isinstance(rows, list):
    rows = []
lines.append(f"--- signal log (newest {len(rows)}) ---")
failsafe_seen = 0
for r in rows:
    reason = str(r.get("reason") or "")
    if "อ่านสถานะไม้เปิด" in reason:
        failsafe_seen += 1
    lines.append(f"{r.get('created_at')} {r.get('event')} "
                 f"{r.get('asset')} {r.get('direction')} "
                 f"ticket={r.get('ticket')} | {reason[:90]}")
lines.append(f"fail-safe blocks seen since deploy: {failsafe_seen}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("DONE — report written")
