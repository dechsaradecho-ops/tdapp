"""Poll prod until the GBPUSD position closes (position_guard fix live). Re-login each cycle."""
import json, time, urllib.request

BASE = "https://tdapp-api.onrender.com"

def req(path, payload=None, token=None):
    r = urllib.request.Request(BASE + path, method="POST" if payload is not None else "GET")
    r.add_header("Content-Type", "application/json")
    if token: r.add_header("Authorization", f"Bearer {token}")
    body = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(r, body, timeout=30) as resp:
        return json.loads(resp.read().decode())

deadline = time.time() + 420
last = None
while time.time() < deadline:
    try:
        tok = req("/api/auth/login", {"pin": "777777"})
        tok = tok.get("access_token") or tok.get("token")
        mon = req("/api/trading/monitor", token=tok)
        pos = mon.get("positions") or mon.get("open_positions") or []
        gbp = [p for p in pos if str(p.get("asset", "")).upper() == "GBPUSD"]
        marks = mon.get("marks") or {}
        last = f"open GBPUSD: {len(gbp)} | uPnL: {gbp[0].get('unrealized_pnl') if gbp else '-'} | mark keys sample: {list(marks)[:4]}"
        print(time.strftime("%H:%M:%S"), last)
        if not gbp:
            print(">>> GBPUSD position CLOSED — position_guard fix is live and working")
            break
    except Exception as e:
        print(time.strftime("%H:%M:%S"), "probe error:", e)
    time.sleep(45)
else:
    print("TIMEOUT — last:", last)
