"""Verify prod /api/trading/monitor shows live prices (not entry-pinned).

Waits for the Render deploy to finish (health endpoint), then logs in and
prints open positions: entry vs current_price vs live quote.
Writes a UTF-8 report next to this script.
"""
import json
import time
import urllib.request

BASE = "https://tdapp-api.onrender.com"
PIN = "777777"
OUT = __file__.replace("verify_monitor_prod.py", "monitor_verify_report.txt")


def req(path: str, token: str | None = None, method: str = "GET",
        body: dict | None = None) -> tuple[int, str]:
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
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


# 1) wait for deploy (max ~5 min)
for attempt in range(15):
    code, text = req("/health")
    if code == 200:
        break
    print(f"health {code}, retrying in 20s ({attempt + 1}/15)")
    time.sleep(20)
print(f"health: {code} {text[:200]}")

# 2) login
code, text = req("/api/auth/login", method="POST", body={"pin": PIN})
assert code == 200, f"login failed: {code} {text}"
token = json.loads(text).get("access_token") or json.loads(text).get("token")

# 3) monitor snapshot
code, text = req("/api/trading/monitor", token=token)
snap = json.loads(text)

lines = [f"HTTP {code}", f"as_of: {snap.get('as_of')}"]
lines.append(f"stats: {json.dumps(snap.get('stats', {}), ensure_ascii=False)}")
lines.append("--- open positions ---")
for p in snap.get("open_positions", []):
    diff = (p.get("current_price") or 0) - (p.get("entry_price") or 0)
    pinned = "PINNED-TO-ENTRY" if abs(diff) < 1e-9 else "live-mark OK"
    lines.append(
        f"{p.get('asset')} ticket={p.get('ticket')} "
        f"entry={p.get('entry_price')} current={p.get('current_price')} "
        f"uPnL={p.get('unrealized_pnl')} [{pinned}]")
lines.append("--- recent trades (last 5) ---")
for t in (snap.get("recent_trades") or [])[:5]:
    lines.append(f"{t.get('asset')} {t.get('status')} pnl={t.get('pnl')}")

report = "\n".join(lines)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(report)
print("REPORT_WRITTEN")
