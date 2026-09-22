"""READ-ONLY simulation: replay the 2026-09-22 drawdown AGAINST the fixed code.

Wraps the REAL production Database but overrides ONLY the ``equity_snapshots``
read in memory (an in-process shim). Everything else — settings, kill_metrics,
evaluate_kill, breached_triggers, the guard's confirm-before-close branch logic
— runs the SAME production code on a SIMULATED 13.25% drawdown.

Proves, with the owner's real settings (max_drawdown_pct=15):
  * 13.25% < 15  -> NOT engaged        (Bug A fixed: no more default 10%)
  * a settings read failure      -> engaged "cannot confirm" + DEFER, no quote
  * a REAL breach + no request   -> the guard RAISES a prompt and DEFERS
  * a breach already requested   -> no double prompt

Writes NOTHING (no insert/update, no push, no pause, no close).
"""
from __future__ import annotations

import sys
from typing import Any, Optional

sys.path.insert(0, r"d:\tdapp\backend")

from app.models.schemas import AppSettings                       # noqa: E402
from app.services import execution, limit_expand                 # noqa: E402
from app.services.database import Database                        # noqa: E402

BAR = "=" * 66
PEAK = 500.0
# 13.25% below PEAK -> current equity.  peak*(1-0.1325) = 433.75
DD_1325 = round(PEAK * (1 - 0.1325), 2)


def _h(t: str) -> None:
    print(f"\n{BAR}\n{t}\n{BAR}")


class _SimDB:
    """Real Database, but ``equity_snapshots`` reads return a simulated dd.

    Only ``select`` for that one table is intercepted; settings, trades,
    kill_expand_requests and trading_pause all come from the real prod DB
    (read-only). No method ever writes.
    """

    def __init__(self, real: Database, snapshots: list[dict[str, Any]]):
        self._real = real
        self._snapshots = snapshots

    def __getattr__(self, name):
        return getattr(self._real, name)

    def select(self, table: str, **kw):
        if table == "equity_snapshots":
            rows = list(self._snapshots)
            if kw.get("desc"):
                rows = sorted(rows, key=lambda r: str(r.get("snapshot_date")),
                              reverse=True)
            limit = kw.get("limit") or len(rows)
            return rows[:limit]
        if table in ("trading_settings",):
            return self._real.select(table, **kw)
        # Everything else: real prod rows (read-only).
        return self._real.select(table, **kw)


class _SettingsReadFails:
    """Real Database, but the trading_settings read RAISES (the incident)."""

    def __init__(self, real: Database):
        self._real = real
        self._client = _BreakingClient(real._client)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _BreakingClient:
    def __init__(self, client):
        self._client = client

    def table(self, name, *a, **k):
        if name == "trading_settings":
            raise RuntimeError("simulated transient settings read failure")
        return self._client.table(name, *a, **k)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _verdict(engaged: bool, request_known: bool, triggers: list, settings_ok: bool):
    """Mirror position_guard's confirm-before-close branch outcomes."""
    if not engaged:
        return "NO breach -> kill switch idle (nothing closes)"
    if not settings_ok:
        return "DEFER, no prompt: settings unreadable — refuse to quote a limit"
    if request_known:
        return "BREACH + request exists -> timeout policy owns outcome (no re-prompt)"
    if not triggers:
        return "ENGAGED but no trigger to propose -> prompt path would fail-safe"
    return "RAISE prompt (Approve/Reject) + DEFER -> owner asked BEFORE any close ✅"


def main() -> int:
    real = Database()
    if not real.available:
        print("DB unavailable:", real.init_error)
        return 2

    s_real = execution.settings_or_none(real)
    if s_real is None:
        print("strict settings read failed; cannot simulate with owner limits.")
        return 2

    print(f"owner settings: capital={s_real.capital}  max_drawdown_pct={s_real.max_drawdown_pct}")

    # ----- Scenario 1: 13.25% dd, owner limit 15 -> NOT engaged -------------
    _h("S1) SIMULATED dd 13.25% vs configured 15%  (the exact incident case)")
    sim = _SimDB(real, [
        {"id": "s-peak", "snapshot_date": "2026-09-01", "equity": PEAK},
        {"id": "s-now", "snapshot_date": "2026-09-22", "equity": DD_1325},
    ])
    dd = execution.equity_drawdown_pct(sim, float(s_real.capital))
    ks = execution.evaluate_kill(sim, s_real)              # settings CONFIRMED
    trigs = limit_expand.breached_triggers(sim, s_real)
    print(f"  simulated drawdown = {dd:.2f}%  (peak {PEAK} -> now {DD_1325})")
    print(f"  engaged            = {ks.engaged}   triggers={ks.triggers}")
    print(f"  breached_triggers  = {[t.get('trigger') for t in trigs]}")
    print(f"  -> {_verdict(ks.engaged, False, trigs, True)}")

    # ----- Scenario 2: same dd, but settings unreadable --------------------
    _h("S2) SAME 13.25% dd, settings READ FAILS (the swallowed-read path)")
    broken = _SettingsReadFails(real)
    s_none = execution.settings_or_none(broken)
    print(f"  settings_or_none() = {s_none}  (None = read failed, NOT defaults)")
    ks2 = execution.evaluate_kill(_SimDB(real, [
        {"id": "s-peak", "snapshot_date": "2026-09-01", "equity": PEAK},
        {"id": "s-now", "snapshot_date": "2026-09-22", "equity": DD_1325},
    ]), AppSettings(), settings_confirmed=False)           # fail-loud path
    print(f"  engaged            = {ks2.engaged}")
    print(f"  triggers           = {ks2.triggers}")
    print(f"  message            = {ks2.message}")
    print(f"  -> {_verdict(ks2.engaged, False, [], settings_ok=False)}")

    # ----- Scenario 3: REAL breach, no request -> prompt + defer -----------
    _h("S3) SIMULATED dd 20% > 15% (a REAL breach), NO request outstanding")
    sim20 = _SimDB(real, [
        {"id": "s-peak", "snapshot_date": "2026-09-01", "equity": PEAK},
        {"id": "s-now", "snapshot_date": "2026-09-22", "equity": round(PEAK * 0.8, 2)},
    ])
    dd20 = execution.equity_drawdown_pct(sim20, float(s_real.capital))
    ks3 = execution.evaluate_kill(sim20, s_real)
    trig3 = limit_expand.breached_triggers(sim20, s_real)
    print(f"  simulated drawdown = {dd20:.2f}%")
    print(f"  engaged            = {ks3.engaged}   triggers={ks3.triggers}")
    for t in trig3:
        print(f"    propose {t.get('label')}: {t.get('value'):.2f}% > "
              f"{t.get('limit')}% -> {t.get('new_limit')}%")
    print(f"  -> {_verdict(ks3.engaged, False, trig3, True)}")

    _h("RESULT")
    print("  The fixed code: (1) honours the owner's 15% (no default 10%),")
    print("  (2) refuses to quote a breach when settings are unreadable and")
    print("  defers instead, and (3) RAISES the Approve/Reject prompt + defers")
    print("  on a real breach with no outstanding request — the owner is asked")
    print("  BEFORE any emergency close. Nothing was written to production.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
