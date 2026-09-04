"""Continuous poll (keeps free-tier instance awake) until GBPUSD closes."""
import json, time, urllib.request

BASE = "https://tdapp-api.onrender.com"

def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

def get(path, tok):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + tok})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

for i in range(40):
    try:
        tok = post("/api/auth/login", {"pin": "777777"})
        tok = tok.get("access_token") or tok.get("token")
        m = get("/api/trading/monitor", tok)
        pos = m.get("open_positions") or []
        gbp = [p for p in pos if p.get("asset") == "GBPUSD"]
        gbp_mark = gbp[0].get("current_price") if gbp else None
        gbp_upnl = gbp[0].get("unrealized_pnl") if gbp else None
        print(f"[{i}] positions={len(pos)} GBPUSD mark={gbp_mark} uPnL={gbp_upnl}", flush=True)
        if not gbp:
            rec = m.get("recent") or []
            closed = [r for r in rec if r.get("asset") == "GBPUSD" and r.get("status") == "closed"]
            print(">>> GBPUSD CLOSED!", flush=True)
            if closed:
                print(">>>", json.dumps(closed[0], ensure_ascii=False)[:400], flush=True)
            break
    except Exception as e:
        print(f"[{i}] error: {e}", flush=True)
    time.sleep(30)
