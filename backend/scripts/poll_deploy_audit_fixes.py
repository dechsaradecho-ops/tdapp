"""Poll prod until the audit-fix deploy (4d610ee) is live, then verify each item.

Read-only. Run from anywhere:  d:\\tdapp\\.venv\\Scripts\\python.exe backend/scripts/poll_deploy_audit_fixes.py
"""
import json
import time
import urllib.error
import urllib.request

BASE = "https://tdapp-api.onrender.com"
WANT = "4d610ee"


def req(path, token=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait_for_deploy():
    for attempt in range(20):  # up to ~10 min
        try:
            _, text = req("/health")
            health = json.loads(text)
        except Exception as exc:  # noqa: BLE001 - transient network
            print(f"  attempt {attempt + 1}: health error {exc}")
            time.sleep(30)
            continue
        commit = str(health.get("commit") or "")
        if commit == WANT:
            print(f"DEPLOY_LIVE commit={commit} (attempt {attempt + 1})")
            return health
        print(f"  attempt {attempt + 1}: commit={commit or '?'} != {WANT}, waiting 30s")
        time.sleep(30)
    return None


health = wait_for_deploy()
if health is None:
    print("TIMEOUT: deploy not detected")
    raise SystemExit(1)

print("\n--- /health ---")
print(json.dumps({k: health.get(k) for k in
                  ("status", "commit", "jobs", "job_next", "job_heartbeat")},
                 ensure_ascii=False)[:1200])

code, text = req("/api/auth/login", method="POST", body={"pin": "777777"})
body = json.loads(text)
token = body.get("access_token") or body.get("token")
print(f"\nlogin HTTP {code} token={'yes' if token else 'NO'}")

fails = []


def check(name, cond, detail):
    print(f"[{'OK ' if cond else 'FAIL'}] {name}: {detail}")
    if not cond:
        fails.append(name)


# --- item 4: exact counts + latest timestamps + TTL purge ------------------
code, text = req("/api/system/counts", token=token)
c = json.loads(text) if code == 200 else {}
print("\n--- item 4: /api/system/counts ---")
print(json.dumps(c, ensure_ascii=False)[:900])
check("counts.verdict", c.get("verdict") == "ok", f"verdict={c.get('verdict')}")
check("counts.exact>100", (c.get("market_analysis") or 0) > 100,
      f"market_analysis={c.get('market_analysis')} (old code capped at 100)")
check("counts.latest", bool(c.get("market_analysis_latest")),
      f"latest={c.get('market_analysis_latest')}")

# --- item 2: sl_moved is a first-class signal_log event --------------------
code, text = req("/api/system/signal-logs?days=7", token=token)
sig = json.loads(text) if code == 200 else {}
summary = sig.get("summary") or {}
print("\n--- item 2: /api/system/signal-logs?days=7 ---")
print(json.dumps(summary, ensure_ascii=False)[:900])
check("signal_logs.summary.sl_moved key", "sl_moved" in summary,
      f"sl_moved={summary.get('sl_moved')} opened={summary.get('order_opened')} "
      f"by_event={summary.get('by_event')}")

# --- item 5: readiness withheld below 30 closed trades --------------------
code, text = req("/api/trading/paper-trading", token=token)
paper = json.loads(text) if code == 200 else {}
print("\n--- item 5: /api/trading/paper-trading ---")
print(json.dumps({k: paper.get(k) for k in
                  ("readiness_ready", "readiness_sample", "readiness_min_sample",
                   "live_readiness_score", "ai_coaching")},
                 ensure_ascii=False)[:900])
ready = paper.get("readiness_ready")
sample = paper.get("readiness_sample")
check("paper.readiness fields present", ready is not None and sample is not None,
      f"ready={ready} sample={sample} min={paper.get('readiness_min_sample')}")
if ready is False:
    check("paper.score withheld", (paper.get("live_readiness_score") or 0) == 0.0,
          f"score={paper.get('live_readiness_score')} while ready=False")

# --- item 7: migration 033 columns survived the settings round trip -------
code, text = req("/api/settings", token=token)
st = json.loads(text) if code == 200 else {}
print("\n--- item 7: /api/settings ---")
check("settings.paper_exit_spread_mult", "paper_exit_spread_mult" in st,
      f"exit_mult={st.get('paper_exit_spread_mult')}")
check("settings.paper_commission_per_lot", "paper_commission_per_lot" in st,
      f"commission={st.get('paper_commission_per_lot')}")

# --- item 4 (read path) + dashboard still healthy -------------------------
code, text = req("/api/market/summary", token=token)
ms = json.loads(text) if code == 200 else {}
print("\n--- item 4b: /api/market/summary ---")
check("market.summary HTTP 200", code == 200, f"HTTP {code}")
print(json.dumps({k: ms.get(k) for k in ("regime", "confidence", "sentiment")},
                 ensure_ascii=False)[:400])
opps = ms.get("opportunities") or []
check("market.text/opportunities", bool(ms.get("explanation")),
      f"opportunities={len(opps)}")

code, text = req("/api/trading/monitor", token=token)
mon = json.loads(text) if code == 200 else {}
print("\n--- regression: /api/trading/monitor ---")
check("monitor HTTP 200", code == 200,
      f"HTTP {code} open={len(mon.get('open_positions') or [])}")

# --- item 6: log_maintenance job registered -------------------------------
jobs = str(health.get("jobs") or "")
hb = health.get("job_heartbeat") or {}
print("\n--- item 6: scheduler jobs ---")
print(f"jobs={jobs[:400]}")
check("log_maintenance registered", "log_maintenance" in jobs,
      f"jobs={jobs[:200]}")

print("\n" + ("ALL CHECKS PASSED" if not fails else f"FAILED: {fails}"))
