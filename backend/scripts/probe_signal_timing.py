"""Probe prod for signal timing: health, latest signals, counts."""
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


health = req("/health")
print("== /health ==")
print(json.dumps(health, ensure_ascii=False, indent=1))

tok = req("/api/auth/login", method="POST", body={"pin": "777777"})
token = tok.get("access_token") or tok.get("token")

sigs = req("/api/signals/latest", token=token)
print(f"\n== /api/signals/latest ({len(sigs)} proposals) ==")
for s in sigs:
    print(f"{s.get('asset')} {s.get('direction')} conf={s.get('confidence')} "
          f"entry={s.get('entry')} approved={bool(s.get('approved_at'))} "
          f"created={s.get('created_at')}")

counts = req("/api/system/counts", token=token)
print("\n== /api/system/counts ==")
print(json.dumps(counts, ensure_ascii=False, indent=1))
