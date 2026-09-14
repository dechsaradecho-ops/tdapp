"""Risk-limit expansion — OWNER-confirmed on LINE, never automatic.

Prod 2026-09-14: drawdown 10.06% > 10.00% fired the kill switch, the position
guard closed all 5 open positions (2 of them winners) and Gate 1 blocked every
new order. The behaviour was correct — but the owner had no sanctioned way to
continue trading, and auto-widening a risk limit is exactly the mistake a kill
switch exists to prevent.

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

Only the limits named in the confirmed request are touched, and only by
``EXPAND_STEP_PCT`` per confirmation. The breach math is shared with the kill
switch (``execution.kill_metrics``) so a prompt can never quote numbers that
disagree with the switch that fired.
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
    """The newest PENDING request, or None.

    A request older than the configured window (Settings →
    ``kill_expand_ttl_min``, default 180 min) is retired as ``expired`` — its
    quoted metrics are stale, so an old chat message / popup can never widen a
    limit today; the monitor issues a fresh request with fresh numbers.
    """
    rows = _select(db, filters={"status": "pending"}, limit=1)
    if not rows:
        return None
    row = rows[0]
    ttl = _resolve_ttl(db, settings)
    age = _age_min(row.get("requested_at") or row.get("created_at"))
    if age is not None and age > ttl and not allow_stale:
        _mark(db, row, "expired")
        log.info("kill expand request expired after %.0f min (ttl %.0f)",
                 age, ttl)
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

    req = pending_request(db, settings=settings)
    if not req:
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

    patch = {t["field"]: float(t["new_limit"]) for t in triggers
             if t.get("field") and t.get("new_limit") is not None}
    if not patch:
        return "⚠️ คำขอไม่สมบูรณ์ (ไม่ระบุลิมิตที่ต้องขยาย) — ยกเลิกคำขอนี้แล้ว"

    s = execution.get_app_settings(db)
    merged = AppSettings.model_validate({**s.model_dump(), **patch})
    if not _persist_settings(db, merged):
        return ("⚠️ บันทึกลิมิตใหม่ไม่สำเร็จ — ลิมิตเดิมยังมีผลและเทรดยังหยุดอยู่\n"
                "ตรวจว่า trading_settings เขียนได้ แล้วกดอนุมัติอีกครั้ง")

    _mark(db, req, "approved", decided_by)
    _audit(db, "limit_expanded", req, triggers, approved=True)

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
        approved=True, applied=triggers, remaining=remaining,
        kill_engaged=kill.engaged or bool(remaining),
        note="; ".join(kill.triggers)[:200])


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
    req = pending_request(db, settings=s)   # may retire a stale row
    live = breached_triggers(db, s)
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
    """Create the request if warranted and push the ONE actionable prompt."""
    res = request_expand(db, s, source=source)
    reason = res.get("reason")
    target = user_id or DEFAULT_USER

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
