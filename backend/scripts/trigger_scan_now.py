"""Trigger scan-now on prod and report the result (worker pipeline smoke test)."""
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
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read().decode())


tok = req("/api/auth/login", method="POST", body={"pin": "777777"})
token = tok.get("access_token") or tok.get("token")

try:
    out = req("/api/system/scan-now", token=token, method="POST")
    print("== scan-now OK ==")
    for r in out.get("results", []) if isinstance(out, dict) else out:
        if isinstance(r, dict):
            opp = r.get("opportunity", {})
            print(f"{r.get('asset')}: score={opp.get('score')} "
                  f"freq_blocked={r.get('frequency_blocked', '-')}")
    print("(raw head)", json.dumps(out, ensure_ascii=False)[:400])
except urllib.error.HTTPError as e:
    print("scan-now HTTP", e.code, e.read().decode()[:500])

c = req("/api/system/counts", token=token)
print("\nmarket_analysis rows:", c["market_analysis"], "latest:", c["market_analysis_latest"])
print("signals rows:", c["signals"], "latest:", c["signals_latest"])
