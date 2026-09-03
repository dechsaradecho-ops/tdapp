"""Check scanner freshness + current opportunity scores on prod."""
import json
import urllib.request

BASE = "https://tdapp-api.onrender.com"


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

c = req("/api/system/counts", token=token)
print("market_analysis rows:", c["market_analysis"], "latest:", c["market_analysis_latest"])
print("signals rows:", c["signals"], "latest:", c["signals_latest"])

s = req("/api/market/summary", token=token)
print("\n== /api/market/summary ==")
print(json.dumps(s, ensure_ascii=False, indent=1)[:2000])
