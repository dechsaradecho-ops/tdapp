r"""Monitor the signal + auto-trade system on PROD end-to-end.

Checks (in order):
  1. /health                          — API alive
  2. /api/system/counts               — worker tables fresh? (scanner/news/trades)
  3. /api/signals/latest              — signals fresh? entry vs live_price drift
  4. /api/trading/pause               — manual kill switch state
  5. /api/trading/monitor             — open positions live-marked? stats
  6. /api/system/autotrader-dry-run   — run ONE trade_once cycle, surface gates
  7. /api/system/quote-logs           — external feed health (last calls)

Writes a UTF-8 report next to this script and prints PASS/FAIL per section.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://tdapp-api.onrender.com"
PIN = "777777"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_monitor_report.txt")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def req(path: str, token: str | None = None, method: str = "POST",
        body: dict | None = None) -> tuple[int, str]:
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


def parse_age(iso: str | None) -> float | None:
    """Minutes since iso timestamp (None if unparseable)."""
    if not iso:
        return None
    try:
        ts = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return None


def fmt_age(mins: float | None) -> str:
    if mins is None:
        return "?"
    if mins < 60:
        return f"{mins:.0f}m"
    if mins < 1440:
        return f"{mins / 60:.1f}h"
    return f"{mins / 1440:.1f}d"


lines: list[str] = []
verdicts: list[tuple[str, str]] = []  # (section, PASS/FAIL/WARN)


def log(s: str = "") -> None:
    lines.append(s)
    print(s)


# 1) health ---------------------------------------------------------------
code, text = req("/health", method="GET")
log(f"=== 1. HEALTH === HTTP {code} {text[:120]}")
verdicts.append(("health", "PASS" if code == 200 else "FAIL"))

# 2) login ----------------------------------------------------------------
code, text = req("/api/auth/login", body={"pin": PIN})
if code != 200:
    log(f"=== LOGIN FAILED === HTTP {code} {text[:200]}")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    sys.exit(1)
token = json.loads(text).get("token") or json.loads(text).get("access_token")
log(f"=== 2. LOGIN === HTTP {code} token={'yes' if token else 'NO'}")
verdicts.append(("login", "PASS" if token else "FAIL"))

# 3) worker table counts ---------------------------------------------------
code, text = req("/api/system/counts", token=token, method="GET")
log(f"=== 3. WORKER TABLES (/api/system/counts) === HTTP {code}")
counts = {}
if code == 200:
    counts = json.loads(text)
    for t in ("market_analysis", "signals", "news_analysis", "trades"):
        latest = counts.get(f"{t}_latest")
        age = parse_age(latest)
        log(f"  {t:<16} rows={counts.get(t)}  latest={latest}  age={fmt_age(age)}")
    ma_age = parse_age(counts.get("market_analysis_latest"))
    sig_age = parse_age(counts.get("signals_latest"))
    verdicts.append(("scanner_fresh", "PASS" if ma_age is not None and ma_age < 20
                     else ("WARN" if ma_age is not None and ma_age < 60 else "FAIL")))
    verdicts.append(("signals_fresh", "PASS" if sig_age is not None and sig_age < 60
                     else "WARN"))
else:
    log(f"  ERROR: {text[:300]}")
    verdicts.append(("scanner_fresh", "FAIL"))
    verdicts.append(("signals_fresh", "FAIL"))

# 4) signals/latest ---------------------------------------------------------
code, text = req("/api/signals/latest", token=token, method="GET")
log(f"=== 4. SIGNALS (/api/signals/latest) === HTTP {code}")
if code == 200:
    sigs = json.loads(text)
    log(f"  count={len(sigs)}")
    feed = sigs[0].get("feed_status", {}) if sigs else {}
    log(f"  feed: state={feed.get('state')} source={feed.get('source')} "
        f"failed={feed.get('failed_assets')}")
    for s in sigs:
        entry = s.get("entry")
        live = s.get("live_price")
        drift = ""
        if isinstance(entry, (int, float)) and isinstance(live, (int, float)) and entry:
            d = (live - entry) / entry * 100
            drift = f" drift={d:+.2f}%"
        log(f"  {s.get('asset'):<7} {s.get('direction'):<4} conf={s.get('confidence'):.2f} "
            f"approval={s.get('approval'):<9} entry={entry} live={live}{drift} "
            f"created={fmt_age(parse_age(s.get('created_at')))} ago"
            + (f" BLOCKED: {s.get('order_blocked')}" if s.get("order_blocked") else ""))
    pend = [s for s in sigs if s.get("approval") == "pending"]
    verdicts.append(("signals_present", "PASS" if sigs else "WARN(none)"))
    verdicts.append(("feed_ok", "PASS" if feed.get("state") == "ok" else "FAIL"))
else:
    log(f"  ERROR: {text[:300]}")
    verdicts.append(("signals_present", "FAIL"))
    verdicts.append(("feed_ok", "FAIL"))

# 5) pause state ------------------------------------------------------------
code, text = req("/api/trading/pause", token=token, method="GET")
pause = json.loads(text) if code == 200 else {}
log(f"=== 5. PAUSE (/api/trading/pause) === HTTP {code} paused={pause.get('paused')} "
    f"reason={pause.get('reason')}")
verdicts.append(("pause_off", "PASS" if not pause.get("paused") else "WARN(paused)"))

# 6) monitor snapshot --------------------------------------------------------
code, text = req("/api/trading/monitor", token=token, method="GET")
log(f"=== 6. MONITOR (/api/trading/monitor) === HTTP {code}")
if code == 200:
    snap = json.loads(text)
    log(f"  stats: {json.dumps(snap.get('stats', {}), ensure_ascii=False)}")
    pinned = 0
    for p in snap.get("open_positions", []):
        diff = (p.get("current_price") or 0) - (p.get("entry_price") or 0)
        is_pinned = abs(diff) < 1e-9
        pinned += 1 if is_pinned else 0
        log(f"  OPEN {p.get('asset'):<7} {p.get('direction'):<4} vol={p.get('volume')} "
            f"entry={p.get('entry_price')} current={p.get('current_price')} "
            f"uPnL={p.get('unrealized_pnl')} [{'PINNED!' if is_pinned else 'live-mark OK'}]")
    for t in (snap.get("recent_trades") or [])[:5]:
        log(f"  TRADE {t.get('asset'):<7} {t.get('status'):<7} pnl={t.get('pnl')} "
            f"closed={fmt_age(parse_age(t.get('closed_at')))} ago")
    n_open = len(snap.get("open_positions", []))
    verdicts.append(("live_marks", "PASS" if (n_open == 0 or pinned == 0)
                     else f"FAIL({pinned}/{n_open} pinned)"))
else:
    log(f"  ERROR: {text[:300]}")
    verdicts.append(("live_marks", "FAIL"))

# 7) autotrader dry-run -------------------------------------------------------
code, text = req("/api/system/autotrader-dry-run", token=token)
log(f"=== 7. AUTOTRADER DRY-RUN (one real trade_once cycle) === HTTP {code}")
if code == 200:
    dr = json.loads(text)
    log(f"  client={dr.get('client')} order_mode={dr.get('order_mode')} "
        f"capital={dr.get('capital')}")
    log(f"  pending_signals={len(dr.get('pending_signals', []))}")
    for p in dr.get("pending_signals", []):
        log(f"    - {p.get('asset')} {p.get('direction')} conf={p.get('confidence')} "
            f"created={fmt_age(parse_age(p.get('created_at')))} ago")
    to = dr.get("trade_once", {})
    log(f"  trade_once: {json.dumps(to, ensure_ascii=False)}")
    log(f"  open_paper_trades={dr.get('open_paper_trades')}")
    for o in dr.get("open_detail", []):
        log(f"    - {o.get('asset')} {o.get('direction')} vol={o.get('volume')} "
            f"ticket={o.get('ticket')}")
    verdicts.append(("autotrader", "PASS" if dr.get("verdict") == "ok" else "FAIL"))
else:
    log(f"  ERROR: {text[:300]}")
    verdicts.append(("autotrader", "FAIL"))

# 8) quote logs (feed health) -------------------------------------------------
code, text = req("/api/system/quote-logs", token=token, method="GET")
log(f"=== 8. QUOTE LOGS (/api/system/quote-logs) === HTTP {code}")
if code == 200:
    try:
        ql = json.loads(text)
        rows = ql if isinstance(ql, list) else ql.get("logs", ql.get("rows", []))
        log(f"  recent calls: {len(rows)}")
        for r in rows[:8]:
            log(f"    {r.get('created_at', '?')[:19]} {r.get('asset', '?'):<7} "
                f"provider={r.get('provider', '?')} status={r.get('status', '?')} "
                f"latency={r.get('latency_ms', '?')}ms")
    except Exception as e:
        log(f"  parse error: {e}")
else:
    log(f"  (skipped: {text[:120]})")

# summary ---------------------------------------------------------------------
log("")
log("=== SUMMARY ===")
for name, v in verdicts:
    log(f"  [{v:<11}] {name}")
fails = [n for n, v in verdicts if v.startswith("FAIL")]
log(f"OVERALL: {'FAIL — ' + ', '.join(fails) if fails else 'ALL PASS'}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(f"report: {OUT}")
