"""Live-style drill: does ONE breach actually reach LINE + web + webpush?

Unlike ``sim_confirm_before_kill.py`` (which only proves the DECISION logic),
this drills the DELIVERY: it takes the owner's REAL production settings, fakes a
breach, then runs the real code path

    limit_expand.request_and_notify(db, settings, NotificationService(...))

with a real ``NotificationService`` wired to a recording LINE client and a
recording Web Push transport. Nothing is sent to the network and NOTHING is
written to production — every write goes to an in-process shim.

It answers the one question the incident raised: "when the kill switch wants to
close, is the owner TOLD on every channel, and is the close actually held?"

Run:  d:\\tdapp\\.venv\\Scripts\\python.exe backend\\scripts\\drill_kill_channels.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations import web_push                      # noqa: E402
from app.models.schemas import AppSettings                 # noqa: E402
from app.services import execution, limit_expand           # noqa: E402
from app.services.database import Database                 # noqa: E402
from app.services.notification_service import (            # noqa: E402
    NotificationService)

CAPITAL = 500.0          # owner's production capital (2026-09-22)
LIMIT = 15.0             # owner's production max_drawdown_pct
DD_PCT = 20.0            # a REAL breach (> 15%) to make the prompt fire


def _iso(mins_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=mins_ago)).isoformat()


class _KVQuery:
    """Chainable supabase stand-in for trading_settings / trading_pause rows."""

    def __init__(self, store: dict, table: str):
        self._store, self._table, self._id = store, table, None

    def select(self, *_a):
        return self

    def eq(self, _col, val):
        self._id = val
        return self

    def limit(self, _n):
        return self

    def upsert(self, row):
        self._store.setdefault(self._table, {})[row.get("id")] = dict(row)
        return self

    def execute(self):
        rows = self._store.get(self._table, {})
        data = ([rows[self._id]]
                if self._id is not None and self._id in rows else [])
        return SimpleNamespace(data=data)


class _KVClient:
    """The `_client.table(...)` surface persist_settings / set_pause use."""

    def __init__(self):
        self.store: dict[str, dict] = {}

    def table(self, name: str) -> _KVQuery:
        return _KVQuery(self.store, name)


class _ShimDB:
    """Minimal Database stand-in: real reads for metrics, recording writes."""

    def __init__(self):
        self.rows: dict[str, list[dict]] = {}
        self.writes: list[tuple[str, str, dict]] = []
        self._client = _KVClient()      # so settings/pause writes SUCCEED
        self.available = True
        # a peak then a drawdown → kill_metrics sees DD_PCT
        now_equity = round(CAPITAL * (1 - DD_PCT / 100.0), 2)
        self.rows["equity_snapshots"] = [
            {"id": "eq-2", "snapshot_date": "2026-09-22", "equity": now_equity},
            {"id": "eq-1", "snapshot_date": "2026-09-01", "equity": CAPITAL},
        ]
        self.rows["kill_expand_requests"] = []
        self.rows["line_targets"] = [{"target_id": "U-owner-group",
                                      "notification_enabled": True}]
        self.rows["push_subscriptions"] = [
            {"id": 1, "endpoint": "https://push.example.com/phone",
             "p256dh": "x", "auth": "y", "enabled": True, "fail_count": 0,
             "last_error": None, "last_ok_at": None,
             "user_agent": "Android Chrome", "created_at": _iso(60)}]

    # --- reads -------------------------------------------------------------
    def select(self, table, filters=None, order="created_at", desc=True,
               limit=50, offset=0, columns="*"):
        rows = list(self.rows.get(table, []))
        for col, val in (filters or {}).items():
            rows = [r for r in rows if r.get(col) == val]
        if table == "equity_snapshots" and order == "snapshot_date":
            rows.sort(key=lambda r: str(r.get("snapshot_date")), reverse=desc)
        return rows[offset:offset + limit]

    def select_ex(self, *a, **k):
        return self.select(*a, **k)

    def count(self, table, filters=None, **k):
        return len(self.select(table, filters=filters, limit=10_000))

    # --- writes (recorded, never real) -------------------------------------
    def insert(self, table, row):
        saved = {**row, "id": f"{table}-{len(self.rows.get(table, [])) + 1}"}
        self.rows.setdefault(table, []).insert(0, saved)
        self.writes.append(("insert", table, dict(row)))
        return row

    def insert_raw(self, table, row):
        self.insert(table, row)
        return dict(self.rows[table][0]), None

    def update(self, table, row_id, changes):
        self.writes.append(("update", table, dict(changes)))
        for r in self.rows.get(table, []):
            if r.get("id") == row_id:
                r.update(changes)
                return True
        return False

    def delete(self, table, filters):
        self.rows[table] = [
            r for r in self.rows.get(table, [])
            if not all(r.get(c) == v for c, v in (filters or {}).items())]
        return True


class _RecLine:
    """Recording LINE client (no network)."""

    def __init__(self):
        self.pushes: list[dict] = []

    async def push(self, target, message, quick_reply=None):
        self.pushes.append({"target": target, "message": message,
                            "quick_reply": quick_reply})
        return True

    async def reply(self, token, message):
        return None


def _sep(title: str) -> None:
    print("\n" + "=" * 66)
    print(title)
    print("=" * 66)


def main() -> int:
    db = _ShimDB()
    s = AppSettings(capital=CAPITAL, max_drawdown_pct=LIMIT)
    line = _RecLine()

    # ---- record the two transports -----------------------------------------
    push_payloads: list[dict] = []

    def fake_send(row, payload):
        push_payloads.append({"endpoint": row["endpoint"], "payload": payload})
        return "ok", ""

    web_push._send_sync = fake_send
    web_push.get_settings = lambda: SimpleNamespace(
        vapid_public_key="pub", vapid_private_key="priv",
        vapid_subject="mailto:drill@example.com")

    svc = NotificationService(db, line)

    # ---- 1. what the kill switch sees --------------------------------------
    _sep("1) KILL SWITCH VIEW (owner's real limits)")
    daily, weekly, monthly, dd = execution.kill_metrics(db, CAPITAL)
    print(f"  capital={CAPITAL}  max_drawdown_pct={LIMIT}")
    print(f"  metrics: daily={daily:.2f}%  weekly={weekly:.2f}%  "
          f"monthly={monthly:.2f}%  drawdown={dd:.2f}%")
    print(f"  breached_triggers = "
          f"{[t['trigger'] for t in limit_expand.breached_triggers(db, s)]}")

    # ---- 2. the prompt path (real service + real notifier) -----------------
    _sep("2) BREACH -> request_and_notify (the real delivery path)")
    res = limit_expand.request_and_notify(db, s, svc)
    print(f"  requested={res.get('requested')}  reason={res.get('reason')}  "
          f"notified={res.get('notified')}")

    _sep("3) CHANNEL: LINE")
    if line.pushes:
        for p in line.pushes:
            first = p["message"].splitlines()[0]
            labels = [b["action"]["label"] for b in (p["quick_reply"] or [])]
            data = [b["action"]["data"] for b in (p["quick_reply"] or [])]
            print(f"  ✓ pushed to {p['target']}")
            print(f"    first line : {first}")
            print(f"    buttons    : {labels}  data={data}")
    else:
        print("  ✗ NO LINE PUSH — the owner was not told!")
        return 1

    _sep("4) CHANNEL: WEB PUSH (OS notification tray)")
    if push_payloads:
        for p in push_payloads:
            payload = json.loads(p["payload"])
            print(f"  ✓ pushed to {p['endpoint']}")
            print(f"    title : {payload['title']}")
            print(f"    body  : {payload['body']}")
            print(f"    url   : {payload['url']}  ntype={payload['ntype']}")
    else:
        print("  ✗ NO WEB PUSH — the phone tray was not alerted!")
        return 1

    _sep("5) CHANNEL: WEB POPUP (what the dashboard would render)")
    req = limit_expand.latest_request(db) or {}
    detail = req.get("detail") or {}
    print(f"  pending status = {req.get('status')}")
    print(f"  trigger        = {req.get('trigger_type')}")
    print(f"  {req.get('limit_before')}% -> {req.get('limit_after')}%"
          f"   (expires in {res and ''}{limit_expand.ttl_minutes(s):.0f} min)")
    print(f"  approve/reject = /dd_ok  /dd_no")
    print(f"  state() pending={limit_expand.state(db, s).get('pending')}")

    # ---- 6. nothing was widened or closed ----------------------------------
    _sep("6) SAFETY: nothing was widened / closed before the answer")
    settings_written = [w for w in db.writes
                        if w[1] == "trading_settings"]
    pause_written = [w for w in db.writes if w[1] == "trading_pause"]
    print(f"  trading_settings writes = {len(settings_written)}  "
          f"(must be 0 before approval)")
    print(f"  trading_pause    writes = {len(pause_written)}  "
          f"(must be 0 before approval)")
    print(f"  request status          = {req.get('status')} (pending)")

    _sep("7) OWNER APPROVES VIA LINE (postback 'dd_ok')")
    reply = limit_expand.handle_postback(db, "dd_ok", decided_by="line:user")
    print(f"  reply: {reply.splitlines()[0]}")
    print(f"  request status now      = "
          f"{db.rows['kill_expand_requests'][0].get('status')}")
    written = db._client.store.get("trading_settings", {}).get(1, {})
    print(f"  new limit written       = "
          f"max_drawdown_pct {written.get('max_drawdown_pct')}%")
    pause_row = db._client.store.get("trading_pause", {}).get(1, {})
    print(f"  pause lifted            = paused={pause_row.get('paused')}")
    print(f"  kill switch after widen = "
          f"engaged={execution.evaluate_kill(db, s).engaged}")

    ok = bool(line.pushes and push_payloads
              and not settings_written and not pause_written
              and written.get("max_drawdown_pct") == LIMIT + 5.0)
    _sep("DRILL VERDICT")
    if ok:
        print("  ✅ ONE breach reached LINE + Web Push, the web popup reads the")
        print("     same request, and NOTHING was widened or closed until the")
        print("     owner answered. Confirm-before-kill holds on all channels.")
    else:
        print("  ✗ a channel did not fire, or a write leaked before approval.")
    print("  (no production row, no network push, no position closed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
