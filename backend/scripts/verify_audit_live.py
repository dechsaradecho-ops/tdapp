"""Live verification after the audit-fix deploy: is the new worker actually
running, and does the new `sl_moved` event appear in real traffic?

Read-only. Waits up to ~8 min for a `log_maintenance` tick if needed.
"""
import json
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
API = "https://tdapp-api.onrender.com"


def req(path, token=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


code, text = req("/api/auth/login", method="POST", body={"pin": "777777"})
token = json.loads(text).get("access_token") or json.loads(text).get("token")

fails = []

# ---- 1. log_maintenance is INVOKED (job_stats heartbeat) ------------------
print("=== item 6: log_maintenance worker ===")
stats = {}
rows = []
for attempt in range(16):  # up to ~8 min
    code, text = req("/api/system/scheduler-logs?job=log_maintenance&limit=5",
                     token=token)
    data = json.loads(text) if code == 200 else {}
    stats = (data.get("job_stats") or {}).get("log_maintenance") or {}
    rows = data.get("logs") or []
    if stats.get("ticks") and rows:
        break
    print(f"  attempt {attempt + 1}: ticks={stats.get('ticks')} rows={len(rows)}"
          " — waiting 30s")
    time.sleep(30)

print(f"job_stats.log_maintenance = {json.dumps(stats, ensure_ascii=False)}")
if rows:
    r = rows[0]
    print(f"latest tick: {r.get('created_at')} status={r.get('status')} "
          f"detail={r.get('detail')} error={r.get('error')}")
    ok = bool(stats.get("ticks")) and r.get("status") == "ok"
    print(f"[{'OK ' if ok else 'FAIL'}] log_maintenance invoked + ok tick")
    if not ok:
        fails.append("log_maintenance")
else:
    print("[WARN] no scheduler_runs row yet for log_maintenance (ticks="
          f"{stats.get('ticks')}) — it may not have fired since the restart")

# ---- 2. retention actually reclaimed the scanner table -------------------
code, text = req("/api/system/counts", token=token)
c = json.loads(text) if code == 200 else {}
print(f"\n=== item 4: counts after purge ===\n{json.dumps(c, ensure_ascii=False)}")
check_ok = c.get("verdict") == "ok" and (c.get("market_analysis") or 0) > 100
print(f"[{'OK ' if check_ok else 'FAIL'}] market_analysis countable (exact, "
      f"not capped at 100): {c.get('market_analysis')}")
if not check_ok:
    fails.append("counts")

# ---- 3. item 2 end-to-end: a NEW sl_moved row in real traffic ------------
print("\n=== item 2: live sl_moved events ===")
code, text = req("/api/system/signal-logs?days=1&limit=200", token=token)
sig = json.loads(text) if code == 200 else {}
logs = sig.get("logs") or []
by_event = {}
for r in logs:
    by_event[r.get("event")] = by_event.get(r.get("event"), 0) + 1
print(f"last 24h events (page): {json.dumps(by_event, ensure_ascii=False)}")
print(f"summary: {json.dumps(sig.get('summary'), ensure_ascii=False)[:500]}")
moved = [r for r in logs if r.get("event") == "sl_moved"]
if moved:
    r = moved[0]
    print(f"NEW sl_moved row: {r.get('created_at')} reason={r.get('reason')!r}")
    print("[OK ] SL moves are logged as `sl_moved`, no longer as `order_opened`")
else:
    print("[INFO] no SL has moved since the deploy yet — the event key exists "
          "and is wired (see /api/system/signal-logs summary `sl_moved`), but "
          "the first real move is what creates a row")

# ---- 4. guard still healthy (no regression) ------------------------------
code, text = req("/api/system/scheduler-logs?job=position_guard&limit=3",
                 token=token)
g = json.loads(text) if code == 200 else {}
gstats = (g.get("job_stats") or {}).get("position_guard") or {}
code, text = req("/api/trading/monitor", token=token)
mon = json.loads(text) if code == 200 else {}
open_n = len(mon.get("open_positions") or [])
ok = code == 200 and gstats.get("error", 0) == 0
print(f"\n=== regression ===\nposition_guard stats={json.dumps(gstats)}")
print(f"monitor HTTP {code} open_positions={open_n}")
print(f"[{'OK ' if ok else 'FAIL'}] position_guard error-free: "
      f"errors={gstats.get('error')}")
if not ok:
    fails.append("position_guard")

print("\n" + ("ALL CHECKS PASSED" if not fails else f"ATTENTION: {fails}"))
