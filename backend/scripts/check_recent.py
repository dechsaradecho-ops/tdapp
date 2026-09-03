"""Check recent closed trades + retry spot feed (429 may be transient)."""
import json, time, urllib.request

BASE = "https://tdapp-api.onrender.com"

def req(path, payload=None, token=None):
    r = urllib.request.Request(BASE + path, method="POST" if payload is not None else "GET")
    r.add_header("Content-Type", "application/json")
    if token: r.add_header("Authorization", f"Bearer {token}")
    body = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(r, body, timeout=30) as resp:
        return json.loads(resp.read().decode())

tok = req("/api/auth/login", {"pin": "777777"})
tok = tok.get("access_token") or tok.get("token")

mon = req("/api/trading/monitor", token=tok)
recent = mon.get("recent") or []
print("recent trades:", len(recent))
for t in recent[:5]:
    print("  ", json.dumps(t, default=str)[:220])

for attempt in range(3):
    mon = req("/api/trading/monitor", token=tok)
    fs = mon.get("feed_status") or {}
    print(f"attempt {attempt+1}: feed={fs.get('state')} failed={fs.get('failed_assets')}")
    gbp = [p for p in (mon.get("open_positions") or []) if p.get("asset", "").upper() == "GBPUSD"]
    print("  GBPUSD open:", len(gbp), "| uPnL:", gbp[0].get("unrealized_pnl") if gbp else "-")
    if not gbp:
        print(">>> CLOSED")
        break
    if attempt < 2:
        time.sleep(35)
