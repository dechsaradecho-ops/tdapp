"""Dump SL/TP distances per position from prod_trades_dump.json (diagnostic)."""
import json

d = json.load(open("scripts/prod_trades_dump.json", encoding="utf-8"))

def find_lists(obj, path="root"):
    """Find every list-of-dicts containing entry_price+stop_loss."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            find_lists(v, f"{path}.{k}")
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "entry_price" in obj[0]:
            print(f"== {path} ({len(obj)} rows) ==")
            print(f"{'asset':8} {'dir':4} {'entry':>10} {'sl_dist':>9} {'sl%':>6} {'tp_dist':>9} {'tp%':>6} {'rr':>5}")
            for r in obj:
                try:
                    e, sl, tp = float(r["entry_price"]), float(r["stop_loss"]), float(r["take_profit"])
                except (KeyError, TypeError, ValueError):
                    continue
                sl_d, tp_d = abs(e - sl), abs(tp - e)
                rr = tp_d / sl_d if sl_d else 0.0
                print(f"{r.get('asset', '?'):8} {r.get('direction', '?'):4} {e:>10.5f} {sl_d:>9.5f} {sl_d/e*100:>5.2f}% {tp_d:>9.5f} {tp_d/e*100:>5.2f}% {rr:>5.2f}")
        else:
            for i, v in enumerate(obj):
                find_lists(v, f"{path}[{i}]")

find_lists(d)

# recent (closed) trades — inspect available keys first
recent = d.get("monitor", {}).get("recent", [])
if recent:
    print(f"\n== monitor.recent ({len(recent)} rows) ==")
    print("keys:", sorted(recent[0].keys()))
    print(f"{'asset':8} {'dir':4} {'entry':>10} {'sl_dist':>9} {'sl%':>6} {'tp_dist':>9} {'tp%':>6} {'rr':>5}")
    for r in recent:
        try:
            e = float(r.get("entry_price") or r.get("open_price") or 0)
            sl = float(r.get("stop_loss") or 0)
            tp = float(r.get("take_profit") or 0)
        except (TypeError, ValueError):
            continue
        if not e or not sl:
            continue
        sl_d, tp_d = abs(e - sl), abs(tp - e) if tp else 0.0
        rr = tp_d / sl_d if sl_d else 0.0
        print(f"{r.get('asset', '?'):8} {r.get('direction', '?'):4} {e:>10.5f} {sl_d:>9.5f} {sl_d/e*100:>5.2f}% {tp_d:>9.5f} {tp_d/e*100:>5.2f}% {rr:>5.2f}")
