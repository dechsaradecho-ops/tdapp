"""Risk-limit expansion — the owner is ASKED first, the window decides last.

Prod 2026-09-14: drawdown 10.06% > 10.00% fired the kill switch, the position
guard closed all 5 open positions (2 of them winners) and Gate 1 blocked every
new order. The behaviour was correct — but the owner had no sanctioned way to
continue trading, and auto-widening a risk limit is exactly the mistake a kill
switch exists to prevent. So a breach ASKS first (nothing widens behind the
owner's back, and never silently).

Flow implemented here:

  1. a breach (monitor) creates a PENDING request in ``kill_expand_requests``
     — no limit is touched, trading stays paused
  2. ONE actionable LINE prompt is pushed showing every breached trigger with
     its current value, its current limit and the proposed ``+5pp`` limit, plus
     Approve/Reject quick-reply buttons and the typed fallback ``/dd_ok`` /
     ``/dd_no``
  3. approve → the named limit columns are written, the pause is lifted, the
     kill switch is RE-EVALUATED and the result is pushed back to LINE
     reject  → limits stay exactly as they were and trading stays paused
  4. NO ANSWER before the window (Settings → ``kill_expand_ttl_min``, default
     180 min) → the request is applied as if approved (owner decision: "ถ้า
     confirm หมดอายุให้ดำเนินการขยาย limit เลย") and the result is pushed to
     the SAME channels, so the widening is never silent. Only the monitor may
     perform this write — see ``auto_apply_expired`` vs ``pending_request``.
  5. while such a request is open, the position guard DEFERS its emergency
     exit (``emergency_hold`` / ``EMERGENCY_HOLD_MIN``): the prompt promises
     "ลิมิตยังไม่ถูกแตะต้อง", so the book must not be force-closed out from
     under the owner's finger (prod 2026-09-14: AUDNZD was closed ~6 s after
     the prompt). The deferral is bounded — silence must not disable a safety
     net for the whole window — and SL/TP management keeps running.

Only the limits named in the request are touched, and only by
``EXPAND_STEP_PCT`` per confirmation; a limit is never written DOWN. The breach
math is shared with the kill switch (``execution.kill_metrics``) so a prompt can
never quote numbers that disagree with the switch that fired.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.integrations.line_client import (build_limit_expand_prompt,
                                          build_limit_expand_result)
from app.models.schemas import AppSettings
from app.services import execution
from app.services.execution import DEFAULT_USER

log = logging.getLogger(__name__)

TABLE = "kill_expand_requests"

# "+5% จากค่าเดิม" — the owner picked a flat 5 PERCENTAGE POINT step
# (10.00% → 15.00%), applied to every limit that is currently breached.
EXPAND_STEP_PCT = 5.0

# A prompt quotes live metrics; after this long they are stale and a fresh
# request (with fresh numbers) is required before anything can be widened.
# This is only the FALLBACK for a caller without a settings row — the live
# value is ``AppSettings.kill_expand_ttl_min`` (Settings page, migration 037,
# default 180 min). A test keeps this constant equal to the schema default.
PENDING_TTL_MIN = 180.0

# Sane range for the configurable window. Too short expires the request before
# the owner can even read the LINE message; unbounded would let a month-old
# chat message widen a limit today. 0 / negative clamps to the SHORT end
# (5 min) — never to "no expiry".
MIN_TTL_MIN = 5.0
MAX_TTL_MIN = 10080.0            # 7 days

# Never ask twice inside this window. An explicit REJECT is respected longer
# than an approved-but-still-breached request (which may legitimately ask for
# another +5pp once the new limits are also exceeded).
REASK_COOLDOWN_MIN = 30.0
REASK_AFTER_REJECT_MIN = 120.0

# How long the position guard DEFERS its emergency exit while a confirmation
# request is still open (prod 2026-09-14: the guard closed AUDNZD ~6 SECONDS
# after the prompt appeared, so the owner never had a chance to press Approve —
# the prompt promises "ลิมิตยังไม่ถูกแตะต้อง", and closing the book while the
# owner is deciding breaks that promise).
#
# It is a DEFERRAL, not a disable: after this long the guard protects the
# account as usual even without an answer (silence must never switch the
# safety net off for the whole 180-min window), and the close message says so.
EMERGENCY_HOLD_MIN = 30.0

# Owner decision (2026-09-14): "ถ้า confirm หมดอายุ ให้ดำเนินการขยาย limit เลย".
# An unanswered request is therefore APPLIED once its window lapses instead of
# being dropped, which would leave the account paused until someone presses a
# button that may never come. Flip to False to go back to "expiry = do nothing".
AUTO_APPLY_ON_EXPIRY = True

# ``decided_by`` written by the timeout path (vs "line:user" / "ui"), and the
# headline of the report that path pushes. Both are greppable in prod.
AUTO_DECIDED_BY = "auto:expired"
TIMEOUT_TITLE = "⏳ หมดเวลายืนยัน — ขยายลิมิตให้อัตโนมัติ"
# Same, for a window that lapsed while NO limit is breached any more: the
# request is retired instead of widening a risk limit the account no longer
# needs (the flow is ask-first, never "widen because time ran out").
AUTO_NO_BREACH_BY = "auto:expired-no-breach"

# trigger key, trading_settings column, LINE label, index into kill_metrics()
# Order matters: drawdown is the trigger that caused the 2026-09-14 incident and
# becomes the headline value of the request row.
TRIGGERS: tuple[tuple[str, str, str, int], ...] = (
    ("drawdown", "max_drawdown_pct", "Drawdown", 3),
    ("daily", "kill_daily_loss_pct", "Daily loss", 0),
    ("weekly", "kill_weekly_loss_pct", "Weekly loss", 1),
    ("monthly", "kill_monthly_loss_pct", "Monthly loss", 2),
)

APPROVE_WORDS = {"approve", "approved", "dd_ok", "/dd_ok", "yes", "ok",
                 "อนุมัติ"}
REJECT_WORDS = {"reject", "rejected", "dd_no", "/dd_no", "no",
                "ไม่อนุมัติ"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_min(raw: Any) -> Optional[float]:
    dt = _parse_utc(raw)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def ttl_minutes(s: Optional[AppSettings] = None) -> float:
    """Confirmation window (minutes) from Settings (``kill_expand_ttl_min``).

    "เวลาในการรอ ตั้งใน setting ได้" — default 180, and both channels (LINE
    prompt + web popup) read the same number, so they expire together. Values
    are clamped into [MIN_TTL_MIN, MAX_TTL_MIN]; missing/unparseable settings
    fall back to ``PENDING_TTL_MIN``.
    """
    raw = getattr(s, "kill_expand_ttl_min", None) if s is not None else None
    if raw is None:
        return PENDING_TTL_MIN
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return PENDING_TTL_MIN
    return max(MIN_TTL_MIN, min(MAX_TTL_MIN, val))


def _resolve_ttl(db, s: Optional[AppSettings] = None) -> float:
    """TTL for callers that only hold a db handle (postback / slash command)."""
    if s is not None:
        return ttl_minutes(s)
    try:
        return ttl_minutes(execution.get_app_settings(db))
    except Exception as exc:
        log.debug("settings lookup for ttl failed, using default: %s", exc)
        return PENDING_TTL_MIN


def _select(db, filters: dict | None = None, limit: int = 5) -> list[dict]:
    """Newest-first rows (never raises — the table may not exist yet)."""
    try:
        return list(db.select(TABLE, filters=filters, order="requested_at",
                              desc=True, limit=limit) or [])
    except Exception as exc:
        log.debug("kill_expand_requests select failed: %s", exc)
        return []


def table_ready(db) -> bool:
    """True when migration 036 is applied (the table can be read).

    Uses the RAISING read path: ``Database.select`` swallows the PostgREST
    "relation does not exist" error into ``[]``, which would make an
    un-migrated database look like a healthy-but-empty table.
    """
    probe = getattr(db, "select_ex", None)
    try:
        if callable(probe):
            probe(TABLE, limit=1)
        else:
            db.select(TABLE, limit=1)
        return True
    except Exception:
        return False


def quick_reply_items() -> list[dict]:
    """LINE quick-reply buttons — same two actions as the typed commands."""
    return [
        {"type": "action", "action": {
            "type": "postback", "label": "✅ อนุมัติ +5%",
            "data": "dd_ok", "displayText": "/dd_ok"}},
        {"type": "action", "action": {
            "type": "postback", "label": "❌ ไม่อนุมัติ",
            "data": "dd_no", "displayText": "/dd_no"}},
    ]


# ---------------------------------------------------------------------------
# breach detection — same numbers as the kill switch
# ---------------------------------------------------------------------------
def breached_triggers(db, s: AppSettings) -> list[dict]:
    """Every limit the account is CURRENTLY over, with the proposed +5pp value.

    Uses ``execution.kill_metrics`` (the switch's own math) and the switch's
    comparison operator (strict ``>``), so a trigger listed here is exactly the
    trigger that engaged — no second, drifting definition of "breached".
    """
    capital = float(getattr(s, "capital", 0) or 0)
    daily, weekly, monthly, drawdown = execution.kill_metrics(db, capital)
    metrics = (daily, weekly, monthly, drawdown)
    out: list[dict] = []
    for key, field, label, idx in TRIGGERS:
        limit = float(getattr(s, field, 0) or 0)
        value = round(float(metrics[idx]), 4)
        if value > limit:
            out.append({
                "trigger": key, "field": field, "label": label,
                "value": value, "limit": limit,
                "new_limit": round(limit + EXPAND_STEP_PCT, 4),
            })
    return out


# ---------------------------------------------------------------------------
# request lifecycle
# ---------------------------------------------------------------------------
def _mark(db, row: dict, status: str, decided_by: str = "") -> None:
    try:
        db.update(TABLE, row.get("id", ""),
                  {"status": status, "decided_at": _now_iso(),
                   "decided_by": decided_by})
    except Exception as exc:
        log.debug("kill_expand_requests update failed: %s", exc)


def latest_request(db) -> Optional[dict]:
    rows = _select(db, limit=1)
    return rows[0] if rows else None


def pending_request(db, allow_stale: bool = False,
                    settings: Optional[AppSettings] = None) -> Optional[dict]:
    """The newest PENDING request that is still inside its window, or None.

    A request older than the configured window (Settings →
    ``kill_expand_ttl_min``, default 180 min) is NOT returned: its quoted
    metrics are stale, so an old chat message / popup can never widen a limit.

    This function is READ-ONLY on purpose (popup poll, GET /limit-expand,
    state()). The window that lapsed is not a dead end any more — the monitor
    APPLIES the request (``auto_apply_expired``, policy ``AUTO_APPLY_ON_EXPIRY``)
    on its own cycle; a passive poll must never widen a limit as a side effect.
    """
    rows = _select(db, filters={"status": "pending"}, limit=1)
    if not rows:
        return None
    row = rows[0]
    if allow_stale:
        return row
    ttl = _resolve_ttl(db, settings)
    age = _age_min(row.get("requested_at") or row.get("created_at"))
    if age is not None and age > ttl:
        log.info("kill expand request is past its window (%.0f min > %.0f) — "
                 "the monitor will apply it", age, ttl)
        return None
    return row


def stale_pending(db, settings: Optional[AppSettings] = None
                  ) -> Optional[dict]:
    """The newest PENDING request that has outlived the configured window.

    Read-only too: it answers "is there something the timeout path owes the
    owner?" without writing. See ``auto_apply_expired`` for the write.

    Only the NEWEST pending row is considered: if a failed write ever leaves an
    old row behind, the next request (fresh numbers, same policy) is the one
    that acts — a queue of stale rows must never stack up +5pp at a time.
    """
    rows = _select(db, filters={"status": "pending"}, limit=1)
    if not rows:
        return None
    row = rows[0]
    ttl = _resolve_ttl(db, settings)
    age = _age_min(row.get("requested_at") or row.get("created_at"))
    if age is None or age <= ttl:
        return None
    return row


def pending_age_min(row: dict) -> float:
    """How long the owner has had this request in hand (minutes).

    0.0 when the timestamp is missing/unparseable — callers use it for messages
    only, never for a decision (``pending_request``/``stale_pending`` treat an
    unreadable age as "cannot tell", not as "brand new").
    """
    return _age_min((row or {}).get("requested_at")
                    or (row or {}).get("created_at")) or 0.0


def open_request(db, settings: Optional[AppSettings] = None
                 ) -> Optional[dict]:
    """The newest request the owner has NOT answered yet (pending, any age).

    Read-only. Unlike ``pending_request`` a lapsed-but-unsettled row still
    counts: the monitor has simply not reached it yet, and the prompt the owner
    is holding is still the live offer.
    """
    row = pending_request(db, settings=settings)
    return row if row is not None else stale_pending(db, settings=settings)


def emergency_hold(db, settings: Optional[AppSettings] = None
                   ) -> Optional[dict]:
    """The open request that MUST put the guard's emergency exit on hold.

    The guard calls this only when the kill switch is already engaged. Returns
    the row to report the deferral with, or None when the guard must close as
    usual (no request at all — e.g. the owner rejected it — or the owner has
    been silent past ``EMERGENCY_HOLD_MIN``).

    Never raises: a DB hiccup must not disable a safety path, so an
    unreadable table means "close" (fail-safe), not "hold".
    """
    try:
        row = open_request(db, settings=settings)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("emergency hold check failed: %s", exc)
        return None
    if row is None:
        return None
    age = _age_min(row.get("requested_at") or row.get("created_at")) or 0.0
    if age > EMERGENCY_HOLD_MIN:
        return None
    return row


def request_expand(db, s: AppSettings, source: str = "monitor") -> dict:
    """Create the pending confirmation request. Nothing is widened here.

    Returns dict(requested=bool, reason=str, triggers=[...], request=row|None).
    reason ∈ {no_breach, already_pending, cooldown, insert_failed, created}.
    """
    triggers = breached_triggers(db, s)
    if not triggers:
        return {"requested": False, "reason": "no_breach", "triggers": []}

    pend = pending_request(db, settings=s)
    if pend:
        return {"requested": False, "reason": "already_pending",
                "request": pend, "triggers": (pend.get("detail") or {}).get(
                    "triggers") or triggers}

    last = latest_request(db)
    if last:
        gap = (REASK_AFTER_REJECT_MIN if last.get("status") == "rejected"
               else REASK_COOLDOWN_MIN)
        age = _age_min(last.get("requested_at") or last.get("created_at"))
        if age is not None and age < gap:
            return {"requested": False, "reason": "cooldown", "request": last,
                    "triggers": triggers or (last.get("detail") or {}).get(
                        "triggers") or []}

    head = triggers[0]
    row = {
        "user_id": DEFAULT_USER,
        "status": "pending",
        "trigger_type": ",".join(t["trigger"] for t in triggers),
        "metric_value": head["value"],
        "limit_before": head["limit"],
        "limit_after": head["new_limit"],
        "detail": {"triggers": triggers, "source": source,
                   "multi": len(triggers) > 1},
        "requested_at": _now_iso(),
    }
    saved = db.insert(TABLE, row)
    fresh = pending_request(db, settings=s)
    if not fresh:
        # DB.insert swallows errors → no row means no approval path either.
        log.error("kill expand request not persisted (run database/036_"
                  "kill_expand_confirm.sql): %s", saved)
        return {"requested": False, "reason": "insert_failed",
                "triggers": triggers, "request": None}
    return {"requested": True, "reason": "created", "request": fresh,
            "triggers": (fresh.get("detail") or {}).get("triggers") or triggers}


def _approve(db, req: dict, decided_by: str, note: str = "", title: str = "",
             settings: Optional[AppSettings] = None) -> tuple[str, str]:
    """Write what the request asked for, lift the pause, re-run the switch.

    Shared by the owner's answer (``decide``) and by the timeout auto-apply, so
    the two paths can never take a different shortcut. Returns ``(reply,
    outcome)`` with outcome ∈ {"applied", "skipped", "failed"}:

    * ``applied`` — the limits were written and the pause was re-evaluated
    * ``skipped`` — nothing to write (a limit already at/above the proposal) and
      the request was closed; safe to report, nothing to retry
    * ``failed``  — the settings write did not land: the request was NOT closed
      so a caller may retry, and nothing must be reported as a widening

    Limits only ever move UP: a request is a snapshot, so when the owner raised
    a limit by hand in the meantime the stale proposal is IGNORED instead of
    silently lowering the newer setting. The report therefore quotes only the
    limits that were actually written, and names the ones it skipped.
    """
    triggers = list((req.get("detail") or {}).get("triggers") or [])
    if not triggers:
        return ("⚠️ คำขอไม่สมบูรณ์ (ไม่ระบุลิมิตที่ต้องขยาย) — ยกเลิกคำขอนี้แล้ว",
                "skipped")

    s = settings or execution.get_app_settings(db)
    patch: dict[str, float] = {}
    for t in triggers:
        field = t.get("field")
        if not field or t.get("new_limit") is None:
            continue
        try:
            proposed = float(t["new_limit"])
            current = float(getattr(s, field, 0) or 0)
        except (TypeError, ValueError):
            continue
        if proposed > current:
            patch[field] = proposed

    if not patch:
        _mark(db, req, "approved", decided_by)
        _audit(db, "limit_expanded", req, triggers, approved=True)
        log.info("kill expand approved by %s — limits already at/above the "
                 "proposal, nothing written", decided_by)
        return ("ℹ️ ลิมิตปัจจุบันสูงกว่าที่คำขอเสนออยู่แล้ว — ไม่มีการเขียนทับ\n"
                "ลิมิตเดิมยังมีผล (ตัวเลขในคำขอคือค่าตอนสร้างคำขอ)", "skipped")

    merged = AppSettings.model_validate({**s.model_dump(), **patch})
    if not _persist_settings(db, merged):
        return ("⚠️ บันทึกลิมิตใหม่ไม่สำเร็จ — ลิมิตเดิมยังมีผลและเทรดยังหยุดอยู่\n"
                "ตรวจว่า trading_settings เขียนได้ แล้วกดอนุมัติอีกครั้ง", "failed")

    # Only quote what was ACTUALLY written: a request can carry several
    # triggers, and one of them may have been raised by hand in the meantime.
    written = [t for t in triggers if str(t.get("field") or "") in patch]
    skipped = [t for t in triggers if str(t.get("field") or "") not in patch]
    if skipped:
        note = ((note + "\n") if note else "") + (
            "ℹ️ ไม่เขียนทับ: " + ", ".join(
                str(t.get("label") or t.get("trigger") or "?") for t in skipped)
            + " (ลิมิตปัจจุบันสูงกว่าที่คำขอเสนออยู่แล้ว)")

    _mark(db, req, "approved", decided_by)
    _audit(db, "limit_expanded", req, written, approved=True)

    # Spec: "อัปเดต max_drawdown + resume ทันที" — resume happens BEFORE the
    # re-evaluation, then the fresh kill state decides whether it stays lifted.
    execution.set_pause(db, False, "")
    kill = execution.evaluate_kill(db, merged)
    remaining = breached_triggers(db, merged)
    if kill.engaged or remaining:
        # Still over a limit (or infra fail-safe) → the gate would block anyway;
        # keep the pause so the UI never claims trading is live.
        execution.set_pause(
            db, True, f"kill switch after expand: {'; '.join(kill.triggers)[:150]}")
    log.warning("kill expand APPROVED by %s: %s → kill engaged=%s remaining=%s",
                decided_by, patch, kill.engaged,
                [t["trigger"] for t in remaining])
    return build_limit_expand_result(
        approved=True, applied=written, remaining=remaining,
        kill_engaged=kill.engaged or bool(remaining),
        note=note or "; ".join(kill.triggers)[:200], title=title), "applied"


def auto_apply_expired(db, settings: Optional[AppSettings] = None
                       ) -> Optional[str]:
    """NO ANSWER BEFORE THE WINDOW LAPSED → apply the expansion anyway.

    Owner decision: "ถ้า confirm หมดอายุ ให้ดำเนินการขยาย limit เลย". The request
    the owner was shown is executed as if approved and the same report is pushed
    to the same channels, so a limit never widens silently.

    Returns the LINE report for a SETTLED window (limits written, or nothing to
    write), else None (nothing was pending, the policy is off, the request is
    unusable, or the settings write failed — the row is then kept so the next
    cycle retries instead of pushing the same failure every minute).

    Only mutating callers may call this (``portfolio_monitor`` via
    ``request_and_notify``, and ``decide`` when the owner answers late). Read
    paths (``state``, GET /api/trading/limit-expand, the popup poll) must not:
    a poll would otherwise widen a limit as a side effect.
    """
    if not AUTO_APPLY_ON_EXPIRY:
        return None
    row = stale_pending(db, settings)
    if row is None:
        return None

    s = settings or execution.get_app_settings(db)
    triggers = list((row.get("detail") or {}).get("triggers") or [])
    if not triggers:
        # A request with no quotable limit can never widen anything: retire it
        # (with a decided_by) so it stops being a candidate every cycle.
        _mark(db, row, "expired", AUTO_DECIDED_BY)
        log.error("kill expand timeout: request %s lists no triggers — retired",
                  row.get("id"))
        return None

    if not breached_triggers(db, s):
        # The metrics came back inside the limits while the owner was thinking.
        # Widening now would raise a risk limit the account does not need, so
        # the request is retired and reported instead.
        _mark(db, row, "expired", AUTO_NO_BREACH_BY)
        log.info("kill expand timeout: no limit is breached any more — retired "
                 "without widening (request %s)", row.get("id"))
        return ("ℹ️ คำขอขยายลิมิตหมดอายุโดยไม่ต้องขยาย\n"
                "ไม่มีลิมิตที่เกินอยู่แล้ว (ค่ากลับมาอยู่ในกรอบ) — ลิมิตเดิมยังมีผล\n"
                "ถ้าเทรดยังหยุดอยู่ ให้พิมพ์ /resume")

    ttl = ttl_minutes(s)
    age = pending_age_min(row)
    note = (f"⏳ ไม่มีการยืนยันภายใน {ttl:.0f} นาที — ระบบขยายลิมิตให้อัตโนมัติ\n"
            "ปรับเวลาในการรอได้ที่หน้า Settings (รูทีนนี้ทำงานทุก ~1 นาที)")
    reply, outcome = _approve(db, row, AUTO_DECIDED_BY, note=note,
                              title=TIMEOUT_TITLE, settings=s)
    if outcome == "failed":
        # Report NOTHING: it would repeat every minute. The row was not closed,
        # so the next cycle retries once trading_settings is writable again.
        log.error("kill expand auto-apply wrote nothing (request %s)",
                  row.get("id"))
        return None
    if outcome == "skipped":
        log.info("kill expand timeout settled without writing (request %s)",
                 row.get("id"))
        return reply
    log.warning("kill expand AUTO-APPLIED after %.0f min of silence (ttl %.0f)",
                age, ttl)
    return reply


def settle_lapsed_window(db, settings, notifier,
                         user_id: str = "") -> tuple[Optional[str], bool]:
    """Apply a lapsed confirmation window AND report it — one step, no skips.

    The monitor-side entry point (``request_and_notify`` also uses it): every
    caller that may widen on timeout must push the same report, so a widening
    can never happen without the owner being told. Returns
    ``(report, notified)``; ``(None, False)`` when nothing needed settling.
    """
    report = auto_apply_expired(db, settings)
    if not report:
        return None, False
    return report, _dispatch(notifier, user_id or DEFAULT_USER,
                             "limit_expand", report)


def decide(db, decision: str, decided_by: str = "line",
           settings: Optional[AppSettings] = None) -> str:
    """Apply an owner decision to the pending request; returns the LINE reply.

    approve → write the requested limits, lift the pause, re-run the kill
              switch, (re)pause again if it is still engaged, report back
    reject  → limits untouched, pause kept, report back

    ``settings`` is the live AppSettings when the caller already has them
    (trading router / monitor); otherwise the window is loaded from the DB.
    """
    word = (decision or "").strip().lower()
    approve = word in APPROVE_WORDS
    if not approve and word not in REJECT_WORDS:
        return ("ไม่เข้าใจคำสั่ง — ใช้ /dd_ok (อนุมัติ) หรือ /dd_no (ไม่อนุมัติ)")

    # An unanswered request that is past its window is APPLIED (policy above),
    # even when the owner answers late — that press must not silently do nothing.
    # This runs BEFORE the read below: pending_request() only reports requests
    # that are still inside their window.
    applied = auto_apply_expired(db, settings)
    if applied:
        return applied

    req = pending_request(db, settings=settings)
    if not req:
        last = latest_request(db)
        if str((last or {}).get("decided_by") or "") == AUTO_DECIDED_BY:
            return ("⏳ คำขอนี้หมดเวลายืนยันและระบบขยายลิมิตไปอัตโนมัติแล้ว\n"
                    f"• ลิมิตล่าสุดที่เขียนไป: {last.get('limit_before')}% → "
                    f"{last.get('limit_after')}%\n"
                    "ถ้าไม่ต้องการ ให้แก้ลิมิตกลับได้ที่หน้า Settings")
        ttl = _resolve_ttl(db, settings)
        return ("ℹ️ ไม่มีคำขอขยายลิมิตที่รอการยืนยันอยู่\n"
                "ถ้ายังเกินลิมิต ระบบจะส่งคำขอใหม่ให้อัตโนมัติ "
                f"(คำขอเดิมมีอายุ {ttl:.0f} นาที)")

    triggers = list((req.get("detail") or {}).get("triggers") or [])
    if not approve:
        _mark(db, req, "rejected", decided_by)
        _audit(db, "limit_expand_rejected", req, triggers, approved=False)
        execution.set_pause(db, True, "risk limits kept — owner rejected expansion")
        log.info("kill expand REJECTED by %s (%s)", decided_by,
                 req.get("trigger_type"))
        return build_limit_expand_result(
            approved=False, applied=[], remaining=triggers, kill_engaged=True,
            note="ลิมิตเดิมยังมีผล — เทรดยังหยุดอยู่ (ใช้ /resume ไม่ได้จนกว่าจะขยาย)")

    reply, _ = _approve(db, req, decided_by, settings=settings)
    return reply


def handle_postback(db, data: str, decided_by: str = "line",
                    settings: Optional[AppSettings] = None) -> Optional[str]:
    """Map a LINE postback payload to a decision. None = not ours."""
    payload = (data or "").strip()
    if payload not in ("dd_ok", "dd_no"):
        return None
    return decide(db, payload, decided_by, settings=settings)


def _public(row: Optional[dict],
            ttl_min: float = PENDING_TTL_MIN) -> Optional[dict]:
    """Trim a raw table row to the fields the UI/LINE surface needs."""
    if not row:
        return None
    requested = row.get("requested_at") or row.get("created_at")
    age = _age_min(requested)
    expires = None
    dt = _parse_utc(requested)
    if dt is not None:
        expires = (dt + timedelta(minutes=ttl_min)).isoformat()
    return {
        "id": row.get("id"),
        "status": str(row.get("status") or ""),
        "trigger_type": str(row.get("trigger_type") or ""),
        "metric_value": row.get("metric_value"),
        "limit_before": row.get("limit_before"),
        "limit_after": row.get("limit_after"),
        "requested_at": requested,
        "expires_at": expires,
        "age_min": None if age is None else round(age, 1),
        "decided_at": row.get("decided_at"),
        "decided_by": str(row.get("decided_by") or ""),
    }


def state(db, s: AppSettings, paused: bool = False,
          pause_reason: str = "") -> dict:
    """Everything the web popup renders — SAME condition as the LINE prompt.

    The popup appears exactly while ``pending`` is true, i.e. while the very
    same ``kill_expand_requests`` row that produced the LINE prompt is still
    awaiting an answer. Triggers are the request's quoted numbers (or, with no
    request, the live breach the monitor is about to quote), so the two
    channels can never show different limits. The waiting window (ttl_min) is
    the Settings value too, so both channels expire at the same moment.
    """
    ttl = ttl_minutes(s)
    req = pending_request(db, settings=s)   # read-only; a lapsed row is applied
    live = breached_triggers(db, s)         # by the monitor, never by this poll
    last = latest_request(db)
    triggers = list((req.get("detail") or {}).get("triggers") or []) if req else list(live)
    last_public = _public(last, ttl)
    if last_public and last_public["status"] == "pending":
        last_public = None              # the pending row is reported separately
    return {
        "pending": bool(req),
        "breach": bool(live or triggers),
        "triggers": triggers,
        "request": _public(req, ttl),
        "last": last_public,
        "paused": bool(paused),
        "pause_reason": str(pause_reason or ""),
        # No pending row although a limit is over → the request could not be
        # stored; same message the LINE prompt carries.
        "setup_required": (not req) and bool(live) and not table_ready(db),
        "step_pct": EXPAND_STEP_PCT,
        "ttl_min": ttl,
        "approve_command": "/dd_ok",
        "reject_command": "/dd_no",
    }


# ---------------------------------------------------------------------------
# notify (monitor calls this on every breach)
# ---------------------------------------------------------------------------
def request_and_notify(db, s: AppSettings, notifier, user_id: str = "",
                       source: str = "monitor") -> dict:
    """Settle any lapsed window, then create/push the ONE actionable prompt.

    This is the monitor's entry point on every breach, i.e. the ONE place a
    lapsed confirmation window is allowed to become a limit write (see
    ``auto_apply_expired``). The result of that write is pushed first, then the
    cycle continues: with the limits now higher the breach may already be gone,
    in which case ``request_expand`` simply reports ``no_breach``.
    """
    target = user_id or DEFAULT_USER
    applied, notified = settle_lapsed_window(db, s, notifier, target)
    if applied:
        return {"requested": False, "reason": "auto_applied",
                "auto_applied": True, "triggers": [],
                "request": latest_request(db), "notified": notified}

    res = request_expand(db, s, source=source)
    reason = res.get("reason")

    if res.get("requested"):
        text = build_limit_expand_prompt(res["triggers"],
                                         ttl_min=ttl_minutes(s))
        res["notified"] = _dispatch(notifier, target, "limit_expand", text,
                                    quick_reply_items())
        return res

    if reason in ("already_pending", "cooldown"):
        res["notified"] = False          # one prompt per window — no repeat spam
        return res

    if reason == "insert_failed":
        res["notified"] = _dispatch(
            notifier, target, "limit_expand",
            build_limit_expand_prompt(res["triggers"], setup_required=True,
                                      ttl_min=ttl_minutes(s)))
    return res


def _dispatch(notifier, user_id: str, ntype: str, message: str,
              quick_reply: Optional[list[dict]] = None) -> bool:
    """Send through the shared NotificationService (sync OR async caller).

    Same loop handling as portfolio_monitor: scheduler thread → asyncio.run,
    running loop → fire-and-forget task. Never raises — a failed prompt must
    not take down the worker that is protecting the account.
    """
    if notifier is None:
        return False
    try:
        import asyncio
        try:
            coro = notifier.notify(user_id, ntype, message,
                                   quick_reply=quick_reply)
        except TypeError:
            # notifier without quick_reply support (older fakes / callers)
            coro = notifier.notify(user_id, ntype, message)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is None:
            asyncio.run(coro)
        else:
            task = running.create_task(coro)
            task.add_done_callback(
                lambda t: t.exception() and log.error(
                    "limit expand notify failed: %s", t.exception()))
        return True
    except Exception as exc:
        log.error("limit expand notify failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# persistence / audit
# ---------------------------------------------------------------------------
def _persist_settings(db, merged: AppSettings) -> bool:
    """Write the full merged settings row (id=1). Delegates to the settings
    router so the table name + row shape have ONE owner.

    The four limit columns have existed since migration 006, so this path needs
    no PGRST204 retry (unlike the interactive PUT /settings save).
    """
    try:
        from app.api.routes.settings import persist_settings
        return persist_settings(db, merged)
    except Exception as exc:
        log.error("persist expanded limits failed: %s", exc)
        return False


def _audit(db, event_type: str, req: dict, triggers: list[dict],
           approved: bool) -> None:
    """risk_events row (same shape monitor writes for limit_breach).

    DB.insert swallows errors, and the authoritative audit trail is the
    kill_expand_requests row itself (status/decided_at/decided_by/detail) —
    this row is for the existing risk-event views.
    """
    try:
        db.insert("risk_events", {
            "user_id": DEFAULT_USER,
            "event_type": event_type,
            "detail": {
                "request_id": req.get("id"),
                "approved": approved,
                "triggers": triggers,
                "requested_at": req.get("requested_at"),
                "limit_before": req.get("limit_before"),
                "limit_after": req.get("limit_after"),
            },
        })
    except Exception as exc:
        log.debug("risk_events insert failed: %s", exc)
