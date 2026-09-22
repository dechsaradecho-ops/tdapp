"""READ-ONLY probe: would the Emergency Exit ask BEFORE closing? (2026-09-22)

Forensic re-run of the incident WITHOUT touching the account: it evaluates the
SAME shared functions the guard and the monitor call, so we can see whether a
breach today would raise the "confirm before kill switch" prompt and defer — or
whether the guard would be free to close.

Writes NOTHING: no insert/update/upsert, no notification push, no pause, no
close. It only reads (trading_settings, equity_snapshots, paper_trades,
kill_expand_requests, trading_pause) and REPORTS.

Usage (PowerShell, from d:\\tdapp):
    $env:PYTHONIOENCODING="utf-8"
    d:\\tdapp\\.venv\\Scripts\\python.exe backend\\scripts\\probe_confirm_before_kill.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, r"d:\tdapp\backend")

from app.services import execution, limit_expand            # noqa: E402
from app.services.database import Database                   # noqa: E402

BAR = "=" * 66


def _h(title: str) -> None:
    print(f"\n{BAR}\n{title}\n{BAR}")


def main() -> int:
    db = Database()

    _h("1) DB / settings read (the 2026-09-22 failure point)")
    if not db.available:
        print("  ✗ DB unavailable:", db.init_error)
        print("    -> a SAFETY read here returns None; the guard would HOLD,")
        print("       not close (fail-safe). Cannot probe further without DB.")
        return 2

    s = execution.settings_or_none(db)
    if s is None:
        print("  ✗ strict settings read FAILED -> settings_ok=False")
        print("    -> guard would DEFER (no prompt, no close) and retry next cycle.")
        return 2
    print(f"  ✓ settings read OK  (strict loader returned a real AppSettings)")
    print(f"    capital            = {s.capital}")
    print(f"    max_drawdown_pct   = {s.max_drawdown_pct}   <- the owner's real limit")
    print(f"    kill_daily_loss_pct= {s.kill_daily_loss_pct}")
    print(f"    kill_weekly/monthly= {s.kill_weekly_loss_pct} / {s.kill_monthly_loss_pct}")
    print(f"    kill_expand_ttl_min= {getattr(s, 'kill_expand_ttl_min', '?')}")
    print(f"    kill_expand_auto   = {getattr(s, 'kill_expand_auto_apply', '?')}")

    _h("2) Drawdown the KILL SWITCH sees (execution.kill_metrics)")
    daily, weekly, monthly, dd = execution.kill_metrics(db, float(s.capital))
    print(f"  daily={daily:.2f}%  weekly={weekly:.2f}%  monthly={monthly:.2f}%  drawdown={dd:.2f}%")
    print(f"  vs limits: daily>{s.kill_daily_loss_pct}  weekly>{s.kill_weekly_loss_pct}  "
          f"monthly>{s.kill_monthly_loss_pct}  drawdown>{s.max_drawdown_pct}")

    _h("3) Shared evaluate_kill (what the GUARD would decide)")
    ks = execution.evaluate_kill(db, s, settings_confirmed=True)
    print(f"  engaged = {ks.engaged}")
    for t in (ks.triggers or []):
        print(f"    - {t}")
    print(f"  message = {ks.message}")

    _h("4) breached_triggers (what the PROMPT would propose)")
    trigs = limit_expand.breached_triggers(db, s)
    if not trigs:
        print("  (none) -> no breach, so the guard would neither close nor prompt.")
    for t in trigs:
        print(f"    - {t.get('label')}: {t.get('value'):.2f}% > {t.get('limit')}% "
              f"-> propose {t.get('new_limit')}%")

    _h("5) Existing request state (dedupe: is a prompt already outstanding?)")
    pend = limit_expand.pending_request(db, allow_stale=False, settings=s)
    stale = limit_expand.stale_pending(db, settings=s)
    hold = limit_expand.emergency_hold(db, s)
    print(f"  pending_request = {pend.get('id') if pend else None}")
    print(f"  stale_pending   = {stale.get('id') if stale else None}")
    print(f"  emergency_hold  = {hold.get('id') if hold else None}")

    _h("6) Existing pause state")
    pause = db.select("trading_pause", filters={"id": 1}, order="id", limit=1) or []
    pr = pause[0] if pause else {}
    print(f"  paused = {pr.get('paused')}  reason = {pr.get('reason')}")

    _h("7) VERDICT — confirm-before-kill on the CURRENT book")
    engaged = bool(ks.engaged and not getattr(ks, "settings_confirmed", True) is False)
    request_known = bool(hold is not None or stale is not None)
    if not ks.engaged:
        print("  ✅ NO breach -> the kill switch is idle; nothing would close.")
    elif request_known:
        print(f"  🟡 BREACH but a request already exists (hold/stale) ->")
        print("     the timeout policy owns the outcome; the guard will NOT")
        print("     re-prompt and may close as the owner decided.")
    elif not trigs:
        print("  🔴 ENGAGED but the prompt path found no trigger to propose.")
        print("     The guard would try to raise a request; if the table is")
        print("     broken it FAILS SAFE and closes — inspect kill_expand_requests.")
    else:
        print("  ✅ BREACH + NO request outstanding -> the guard (and monitor)")
        print("     would RAISE the Approve/Reject prompt and DEFER. The owner")
        print("     is asked BEFORE any emergency close. ✅ this is the fix.")
    print("\n  (probe wrote NOTHING — no row, no push, no pause, no close)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
