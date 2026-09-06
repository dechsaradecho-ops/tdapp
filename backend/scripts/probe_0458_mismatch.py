"""Probe: signal-log events around 2026-09-06 04:58 (user report:
log says order NOT opened but an order WAS opened — AUDUSD, XAUUSD)."""
import json
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://tdapp-api.onrender.com"
OUT = "_probe_0458.txt"


def req(path, token=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    tok = req("/api/auth/login", method="POST", body={"pin": "777777"})
    tok = tok.get("access_token") or tok.get("token")
    lines = []

    raw = req("/api/system/signal-logs?limit=200", token=tok)
    logs = raw.get("logs") or raw.get("items") or []
    lines.append(f"signal_logs rows fetched: {len(logs)}")

    # window: 2026-09-06 03:30 – 06:30 UTC (4:58 local report context)
    lo, hi = "2026-09-06T03:30", "2026-09-06T06:30"
    sel = [l for l in logs
           if lo <= str(l.get("created_at", ""))[:16] <= hi]
    lines.append(f"events in window {lo}..{hi}: {len(sel)}")
    for l in sorted(sel, key=lambda x: str(x.get("created_at"))):
        lines.append(
            f"{l.get('created_at', '')[:19]} | {l.get('asset')} "
            f"{l.get('direction')} | {l.get('event')} | "
            f"sid={str(l.get('signal_id'))[:8]} | src={l.get('source')} | "
            f"tk={str(l.get('ticket'))[:16]} | "
            f"{str(l.get('reason'))[:90].replace(chr(10), ' ')}")

    # paper_trades opened in the same window (the real orders)
    mon = req("/api/trading/monitor", token=tok)
    recent = mon.get("recent") or []
    lines.append("")
    lines.append(f"monitor recent trades: {len(recent)}")
    for t in recent[:20]:
        lines.append(json.dumps(t, ensure_ascii=False, default=str)[:200])

    open_pos = mon.get("open_positions") or []
    lines.append("")
    lines.append(f"open_positions now: {len(open_pos)}")
    for p in open_pos:
        lines.append(
            f"{p.get('asset')} {p.get('direction')} vol={p.get('volume')} "
            f"entry={p.get('entry_price')} tk={str(p.get('ticket'))[:16]} "
            f"opened={p.get('opened_at') or p.get('created_at')} "
            f"src={p.get('source')}")

    # signals table rows for AUD/XAU on 09-06
    sigs = req("/api/signals/latest", token=tok)
    lines.append("")
    lines.append(f"signals/latest: {len(sigs)}")
    for s in sigs[:10]:
        lines.append(
            f"{s.get('asset')} {s.get('direction')} conf={s.get('confidence')} "
            f"approval={s.get('approval')} created={str(s.get('created_at'))[:19]} "
            f"approved_at={s.get('approved_at')}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {OUT}: {len(lines)} lines")


if __name__ == "__main__":
    main()
