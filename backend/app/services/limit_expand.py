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
     the SAME channels, so the widening is never silent. Whichever mutating
     caller reaches the lapsed row first performs this write (the monitor, or
     the position guard) — see ``settle_lapsed_window`` vs ``pending_request``.
     HOW MANY times that may happen is the owner's switch
     ``kill_expand_auto_apply`` (Settings → "ขยายอัตโนมัติ 1 ครั้ง", migration
     039): ON (default) = every lapsed window is applied; OFF = exactly ONE
     lapsed window is applied (``AUTO_ONCE_BY``, one rescue per
     ``ONCE_QUOTA_HOURS``) and a LATER one is ``capped`` — nothing is widened,
     the owner is warned once, no new prompt is pushed, and the emergency exit
     is free to close the book ("ขยายแล้วยังไม่พอ = ปิดไม้"): an unanswered
     prompt must not become a permanent exemption from the risk limits. The way
     back in is the Settings page (+ ``/resume``), not a fresh prompt.
     A window that could NOT be applied (the settings write keeps failing) stays
     OPEN so the owner can still press: the failure is reported
     once per ``AUTO_FAIL_NOTIFY_MIN`` (6 h), the popup keeps offering it, and
     the emergency exit keeps deferring (point 5).
  5. while such a request is open, the position guard DEFERS its emergency
     exit (``emergency_hold``): the prompt promises "ลิมิตยังไม่ถูกแตะต้อง", so
     the book must not be force-closed out from under the owner's finger (prod
     2026-09-14: AUDNZD was closed ~6 s after the prompt). Owner decision:
     "ต้องรอคอมเฟิร์มก่อนถึงจะ kill switch ทำงาน" — the guard stands down for the
     WHOLE confirmation window (no grace), and SL/TP management keeps running
     the entire time. When the window lapses the timeout policy decides first
     (point 4); the guard closes only if the account is STILL over the widened
     limits. A window that could not be settled is NOT a reason to close on its
     own — but the one-shot policy's ``capped`` outcome IS ("ขยายแล้วยังไม่พอ =
     ปิดไม้ทันที"), so ``HOLD_KINDS`` covers the write failure only and the close
     message names the outcome whenever a close does happen.

Only the limits named in the request are touched, and only by
``EXPAND_STEP_PCT`` per confirmation; a limit is never written DOWN. The breach
math is shared with the kill switch (``execution.kill_metrics``) so a prompt can
never quote numbers that disagree with the switch that fired.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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

# Extra minutes the position guard keeps deferring its emergency exit AFTER the
# confirmation window has run out: NONE. The wait IS the window
# (Settings → ``kill_expand_ttl_min``, default 180 min) — the guard stands down
# exactly as long as the offer in the owner's hand is alive, and when the window
# lapses the timeout policy decides (see ``settle_lapsed_window``), not the
# guard. Owner decision 2026-09-14: "ต้องรอคอมเฟิร์มก่อนถึงจะ kill switch ทำงาน"
# then "ถ้ารอตาม kill_expand_ttl_min แล้วไม่ได้รับการตอบกลับให้ขยายอัตโนมัติ".
#
# There is no separate cap on purpose: a fixed one (the first fix used 30 min)
# closed the book while the prompt was still valid, and a LARGER one (window +
# grace) closed it after a window whose policy is to expand. The deferral also
# survives the case where the request can NEVER be settled because the settings
# write keeps failing: the owner is still owed a decision there, so the emergency
# exit keeps deferring and re-warns — owner decision 2026-09-14: "ถ้าเขียน DB
# ไม่สำเร็จห้ามปิดไม้". With the one-shot policy OFF a silence that the system
# already answered ONCE is NOT such a case: nothing is owed any more, so the guard
# protects the account as usual (owner decision 2026-09-14: "ขยายแล้วยังไม่พอ =
# ปิดไม้ทันที").

# Owner decision (2026-09-14): "ถ้า confirm หมดอายุ ให้ดำเนินการขยาย limit เลย".
# An unanswered request is therefore APPLIED once its window lapses instead of
# being dropped, which would leave the account paused until someone presses a
# button that may never come. This is only the FALLBACK for a caller without a
# settings row — the live value is ``AppSettings.kill_expand_auto_apply``
# (Settings page, migration 039). Kept for the older callers/tests that pin it.
AUTO_APPLY_ON_EXPIRY = True

# ``decided_by`` written by the timeout path (vs "line:user" / "ui"), and the
# headline of the report that path pushes. Both are greppable in prod.
AUTO_DECIDED_BY = "auto:expired"
# Same, for the ONE-SHOT policy (Settings → kill_expand_auto_apply = OFF): the
# first silence window is still applied for the owner, but only that one. A
# distinct decided_by so the LOGS page can show WHY a limit moved without an
# answer (นโยบายปิด + หมดเวลา) and so the quota scan can tell the two rules apart.
AUTO_ONCE_BY = "auto:once"
# Every decided_by that means "the timeout policy widened this, not the owner".
# Used by ``decide`` to recognise a window that silence already answered (and by
# the older callers that only want "was this one widened?"). NOTE: only
# ``AUTO_ONCE_BY`` spends the one-shot quota — see ``silent_widen_count``.
AUTO_WIDEN_BY = (AUTO_DECIDED_BY, AUTO_ONCE_BY)
# A window the one-shot policy REFUSED to widen: closed exactly like an expired
# request but with its own marker, so the logs can tell "ขยายให้เอง" apart from
# "ไม่ขยายให้ เพราะโควตาหมด" (status is ``expired`` → never counts as a widening
# for ``silent_widen_count``).
AUTO_CAPPED_BY = "auto:capped"
TIMEOUT_TITLE = "⏳ หมดเวลายืนยัน — ขยายลิมิตให้อัตโนมัติ"
# Same, for the one-shot policy: the report says it will not happen again.
ONCE_TITLE = "⏳ หมดเวลายืนยัน — ขยายลิมิตให้ 1 ครั้ง (นโยบายปิด)"
# How many rows are scanned to find out whether the one-shot quota is used up.
# The lookup is a "did any request get widened by the timeout path recently?"
# question, not an audit — 20 rows cover every window inside the 7-day TTL cap.
ONCE_SCAN_ROWS = 20
# How long a silent widening keeps the one-shot quota spent ("ขยายอัตโนมัติ 1
# ครั้ง"). A DAY, because the limits being widened are mostly daily losses: "ครั้ง
# ละ" must give the owner one rescue per breach day, not one for the lifetime of
# the account (a breach a month later would otherwise find the bot mute).
ONCE_QUOTA_HOURS = 24.0
# Same, for a window that lapsed while NO limit is breached any more: the
# request is retired instead of widening a risk limit the account no longer
# needs (the flow is ask-first, never "widen because time ran out").
AUTO_NO_BREACH_BY = "auto:expired-no-breach"

# A window the timeout policy could NOT settle (the settings write keeps failing)
# or MAY NOT apply (the one-shot quota is used up) leaves the account paused with
# nothing but an OLD prompt in the owner's hand. Every other outcome reports
# itself, so these must too — otherwise a limit silently never widens and trading
# stays stopped until the owner happens to look at /monitor. Throttled per request
# id: BOTH workers retry every minute and the row stays pending until something
# succeeds.
AUTO_FAIL_NOTIFY_MIN = 360.0
_FAIL_NOTICES: dict[str, float] = {}

# Settle kinds where the emergency exit must keep DEFERRING: only ``failed`` —
# the settings write did not land, so the owner's (implicit) approval is still
# being honoured and it is retried every cycle. Owner decision 2026-09-14:
# "ถ้าเขียน DB ไม่สำเร็จห้ามปิดไม้". Every other kind is a CLOSED window:
# applied/skipped/no-breach/retired all settle it, and ``capped`` (the one-shot
# quota is used up, so the silence is final) lets the guard protect the account
# exactly like any other breach — owner decision 2026-09-14: "ขยายแล้วยังไม่พอ =
# ปิดไม้ทันที". A kind outside this tuple and outside ``SettleResult.settled``
# falls through to the guard's usual fail-safe close — a bug must not switch the
# safety net off.
HOLD_KINDS = ("failed",)


def _fail_notice_due(request_id: str) -> bool:
    """True when this request may be warned about (first time, then every 6 h)."""
    now = time.monotonic()
    cooldown = AUTO_FAIL_NOTIFY_MIN * 60.0
    last = _FAIL_NOTICES.get(str(request_id or ""))
    if last is not None and (now - last) < cooldown:
        return False
    for key in [k for k, at in _FAIL_NOTICES.items() if (now - at) >= cooldown]:
        _FAIL_NOTICES.pop(key, None)     # never let the dict grow forever
    return True


def _note_fail_notice(request_id: str) -> None:
    """Record a warning that was actually DELIVERED (a failed push may retry)."""
    _FAIL_NOTICES[str(request_id or "")] = time.monotonic()


def _fail_notice(kind: str, ttl: float) -> str:
    """The one-time warning for a window that ran out and was NOT applied.

    Words matter here: NOTHING was widened, so the text never claims it was —
    it says the limits still stand and how to get moving again (the same two
    buttons as the original prompt). Same headline for both causes, so the
    owner learns the one thing that matters ("ขยายอัตโนมัติไม่สำเร็จ") first, and
    a different tail per cause: a failed write is retried (the book is held),
    while a used-up one-shot quota is FINAL (the book may be closed).
    """
    if kind == "capped":
        # The owner's own switch ("ขยายอัตโนมัติ 1 ครั้ง") is why nothing happens
        # now — and the emergency exit is about to do its job, so say it.
        return ("⚠️ ขยายลิมิตอัตโนมัติไม่สำเร็จ\n"
                f"• รอครบ {ttl:.0f} นาที ไม่มีคำตอบ และระบบขยายให้เองได้แค่ "
                "ครั้งเดียว (นโยบายปิด)\n"
                "• ลิมิตเดิมยังมีผล และเทรดยังหยุดอยู่\n"
                "• ครั้งนี้ระบบจะไม่ขยายให้เอง — ปล่อยให้ kill switch ทำงาน "
                "(ปิดไม้เพื่อความปลอดภัย)\n"
                "กดอนุมัติในข้อความเดิมเพื่อทำต่อ หรือแก้ลิมิตที่หน้า Settings")
    why = ("ระบบบันทึกค่าใหม่ไม่ลง (จะลองใหม่ทุกรอบ)" if kind == "failed"
           else "นโยบายขยายอัตโนมัติถูกปิดอยู่")
    # The owner must know the emergency exit is NOT closing the book while we
    # wait, otherwise "ไม่สำเร็จ" reads as "ไม้ถูกปิดเพราะระบบพัง".
    tail = ("• ระบบยังไม่ปิดไม้ (SL/TP ทำงานปกติ) และจะลองบันทึกให้ใหม่ทุกรอบ"
            if kind == "failed" else
            "• ระบบยังไม่ปิดไม้ (SL/TP ทำงานปกติ) — รอคุณกดอนุมัติ/ไม่อนุมัติ")
    return ("⚠️ ขยายลิมิตอัตโนมัติไม่สำเร็จ\n"
            f"• รอครบ {ttl:.0f} นาที ไม่มีคำตอบ และ{why}\n"
            "• ลิมิตเดิมยังมีผล และเทรดยังหยุดอยู่\n"
            f"{tail}\n"
            "กดอนุมัติในข้อความเดิมเพื่อทำต่อ หรือแก้ลิมิตที่หน้า Settings")


def _retired_notice(ttl: float) -> str:
    """A request that quotes no limit at all can never widen anything."""
    return ("⚠️ คำขอขยายลิมิตหมดอายุและถูกยกเลิก\n"
            f"• รอครบ {ttl:.0f} นาที ไม่มีคำตอบ และคำขอไม่มีลิมิตระบุไว้\n"
            "• ลิมิตเดิมยังมีผล — ถ้ายังเกินลิมิต ระบบจะส่งคำขอใหม่ให้เอง")


def build_capped_notice() -> str:
    """The one-time report for "the platform stops asking" (one-shot policy).

    ``request_and_notify`` refuses to create a NEW prompt once the account has
    had its single silent widening, so this is the only message the owner gets
    while the account stays over the old limits: it must say what did NOT happen
    and what does — trading stays paused, the kill switch protects the book, and
    the way forward is Settings (+ /resume). Nothing was widened by this cycle,
    so the text never claims it was.
    """
    return ("⚠️ ครบโควตาขยายลิมิตอัตโนมัติแล้ว (นโยบายขยายอัตโนมัติปิดอยู่)\n"
            "• ไม่มีคำตอบ และระบบขยายให้เองได้แค่ครั้งเดียวใน 24 ชม.\n"
            "• คำขอใหม่จะไม่ถูกส่งให้เองอีกจนครบ 24 ชม. — ลิมิตเดิมยังมีผล "
            "เทรดหยุดอยู่\n"
            "• ยังเกินลิมิต → kill switch ปิดไม้เพื่อความปลอดภัย\n"
            "• ต้องการเทรดต่อ: แก้ลิมิตเองที่หน้า Settings แล้วใช้ /resume\n"
            "• ต้องการให้ขยายเองได้ทุกครั้งที่หมดเวลา → เปิดนโยบายที่หน้า Settings")


@dataclass(frozen=True)
class SettleResult:
    """Outcome of ONE attempt to settle a confirmation window that ran out.

    ``kind`` is the whole story, and callers branch on ``settled`` (NOT on
    "is there a message"): a FAILED attempt now carries ``notice`` — the owner
    must hear about it — while the request is still unsettled and retried.

      applied    limits written, pause re-evaluated, ``report`` pushed
      skipped    a limit already ≥ the proposal → nothing written, ``report``
      no-breach  no limit is over any more → retired WITHOUT widening
      retired    the row quotes no limit → cannot ever widen, row closed
      failed     the settings write did not land → row KEPT, retry next cycle
      capped     the one-shot quota is used up (``kill_expand_auto_apply`` is
                 False and the timeout path already widened once) → nothing was
                 written and nothing will be: the guard may now close the book
      none       no lapsed window to settle (the common case)
    """
    kind: str
    report: str = ""        # LINE report for a SETTLED window ("" otherwise)
    notice: str = ""        # what to tell the owner when it was NOT settled
    notified: bool = False
    request_id: str = ""
    age_min: float = 0.0
    ttl_min: float = PENDING_TTL_MIN

    @property
    def settled(self) -> bool:
        """The window is closed: nothing left to retry, nothing left to hold."""
        return self.kind in ("applied", "skipped", "no-breach", "retired")

    @property
    def holds(self) -> bool:
        """The emergency exit must keep deferring — the expansion is still owed.

        True for ``failed`` only: the settings write keeps failing, the owner's
        (implicit) approval is still being honoured and it is retried every
        cycle. See ``HOLD_KINDS``. ``capped`` does NOT hold — the one-shot policy
        answered this silence already, so the guard closes if the account is
        still over the widened limits.
        """
        return self.kind in HOLD_KINDS

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


def auto_apply_enabled(s: Optional[AppSettings] = None) -> bool:
    """Is the timeout path allowed to widen the limits by itself?

    Settings → ``kill_expand_auto_apply`` (migration 039, default True):

      * True  — EVERY lapsed window is applied ("ขยายอัตโนมัติเมื่อหมดเวลา")
      * False — only the FIRST lapsed window is applied ("ขยายอัตโนมัติ 1 ครั้ง")

    A caller without settings (an un-migrated row, an older caller) falls back
    to ``AUTO_APPLY_ON_EXPIRY`` so today's behaviour is kept.
    """
    raw = getattr(s, "kill_expand_auto_apply", None) if s is not None else None
    if raw is None:
        return AUTO_APPLY_ON_EXPIRY
    return bool(raw)


def silent_widen_count(db) -> int:
    """How many times the ONE-SHOT policy already widened without an answer.

    Answers the only question the one-shot switch needs: "has the system
    already rescued this account by itself inside the current quota window?".
    Counts ``approved`` rows whose ``decided_by`` is ``AUTO_ONCE_BY`` (a
    widening the TIMEOUT path performed while the policy was OFF) among the
    newest ``ONCE_SCAN_ROWS`` requests, and only when it happened within
    ``ONCE_QUOTA_HOURS``. An owner press (``line:*``, ``ui:*``) is NOT a silent
    widening and never spends the quota, and neither does an ``auto:expired``
    row: that one was widened while the policy was ON, i.e. under a rule the
    owner has since changed — the fresh OFF policy still gets its ONE rescue.

    The quota window is why the count is time-bounded: "1 ครั้ง" must mean
    "at most one silent rescue per day", not "never again for this account".
    Never raises — an unreadable table counts as 0, the LENIENT direction that
    can only ever allow the single widening, never a repeated one.
    """
    try:
        rows = _select(db, limit=ONCE_SCAN_ROWS)
    except Exception as exc:          # pragma: no cover - _select swallows too
        log.debug("one-shot quota lookup failed: %s", exc)
        return 0
    total = 0
    for row in rows:
        if str(row.get("status") or "") != "approved":
            continue
        if str(row.get("decided_by") or "") != AUTO_ONCE_BY:
            continue
        age = _age_min(row.get("requested_at") or row.get("created_at"))
        # An age the table cannot answer is treated as INSIDE the window: the
        # fail-safe direction for a risk limit is "do not widen again".
        if age is None or age <= ONCE_QUOTA_HOURS * 60.0:
            total += 1
    return total


def timeout_plan(db, s: Optional[AppSettings] = None) -> str:
    """What the timeout path may do with the lapsed window: apply/once/capped.

    * ``apply`` — the policy is ON: every lapsed window is applied
    * ``once``  — the policy is OFF and the one-shot quota is still available
    * ``capped``— the policy is OFF and the system already rescued this account
                  inside ``ONCE_QUOTA_HOURS``, so this window is NOT applied;
                  the owner is warned and the kill switch may act
    """
    if auto_apply_enabled(s):
        return "apply"
    return "capped" if silent_widen_count(db) > 0 else "once"


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
    state()). The window that lapsed is not a dead end any more — the mutating
    callers settle it (``settle_lapsed_window``: widened, retired, or capped by
    the one-shot policy) on their own cycle; a passive poll must never widen a
    limit as a side effect.
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
    owner?" without writing. See ``settle_expired`` for the write.

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


def emergency_hold(db, settings: Optional[AppSettings] = None
                   ) -> Optional[dict]:
    """The request that put the guard's emergency exit on hold.

    The guard calls this only when the kill switch is already engaged. Returns
    the request the owner is STILL being asked about — a pending row inside its
    confirmation window (``kill_expand_ttl_min``, default 180 min). Such a hold
    is unconditional: the prompt in the owner's hand promises "ลิมิตยังไม่ถูกแตะต้อง".

    None means this call cannot vouch for a hold, for one of three reasons:
    nobody is being asked (no row — e.g. the owner rejected it, which settles
    the row and is a plain "no"), the window has RUN OUT (the timeout policy
    owns it then — see ``settle_lapsed_window``), or we cannot read/date the
    row. None is NOT an instruction to close: the guard then settles the lapsed
    window and closes only if the account is still over the WIDENED limits — a
    window that cannot be settled (write failure) keeps deferring, because the
    owner has not been served yet (owner decision 2026-09-14: "ถ้าเขียน DB
    ไม่สำเร็จห้ามปิดไม้"). A window the one-shot policy refused (``capped``) is
    NOT such a case and is settled/closed by that call.

    Never raises: a DB hiccup must not disable a safety path, so an unreadable
    table means "no hold from this call" (the guard's fail-safe is the close),
    not an exception. Same for a row whose timestamp cannot be parsed — an age
    we cannot measure must not read as "brand new".
    """
    try:
        row = pending_request(db, settings=settings)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("emergency hold check failed: %s", exc)
        return None
    if row is None:
        return None
    if _age_min(row.get("requested_at") or row.get("created_at")) is None:
        log.warning("request %s has no readable timestamp — closing instead "
                    "of holding the emergency exit", row.get("id"))
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
        _audit(db, "limit_expanded", req, triggers, approved=True,
               decided_by=decided_by)
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
    _audit(db, "limit_expanded", req, written, approved=True,
           decided_by=decided_by)

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


def settle_expired(db, settings: Optional[AppSettings] = None) -> SettleResult:
    """NO ANSWER BEFORE THE WINDOW LAPSED → apply the expansion anyway.

    Owner decision: "ถ้า confirm หมดอายุ ให้ดำเนินการขยาย limit เลย". The request
    the owner was shown is executed as if approved and the same report is pushed
    to the same channels, so a limit never widens silently. See ``SettleResult``
    for the kinds; only ``applied`` / ``skipped`` mean a limit was looked at.

    HOW MANY times the silence may widen is the owner's switch
    (``kill_expand_auto_apply``, migration 039 → ``timeout_plan``):

      * ON (default) → ``apply``: this window is applied (``auto:expired``)
      * OFF          → ``once`` when the timeout path has not widened yet
                       (``auto:once`` — "ขยายอัตโนมัติ 1 ครั้ง"), else ``capped``:
                       nothing is written, the owner gets ONE warning, and the
                       row is closed so the emergency exit may protect the
                       account ("ขยายแล้วยังไม่พอ = ปิดไม้ทันที")

    Only mutating callers may call this (``portfolio_monitor`` and
    ``position_guard`` via ``settle_lapsed_window``, and ``decide`` when the
    owner answers late). Read paths (``state``, GET /api/trading/limit-expand,
    the popup poll) must not: a poll would otherwise widen a limit as a side
    effect.
    """
    row = stale_pending(db, settings)
    if row is None:
        return SettleResult("none")
    ttl = _resolve_ttl(db, settings)
    age = pending_age_min(row)
    rid = str(row.get("id") or "")

    def _res(kind: str, report: str = "", notice: str = "") -> SettleResult:
        return SettleResult(kind, report=report, notice=notice, request_id=rid,
                            age_min=age, ttl_min=ttl)

    s = settings or execution.get_app_settings(db)
    plan = timeout_plan(db, s)
    if plan == "capped":
        # One-shot policy ("ขยายอัตโนมัติ 1 ครั้ง") and the quota is used up: the
        # system already widened once instead of an answer, so a SECOND silence
        # is final. The row is closed (with a decided_by) so it stops being a
        # candidate every cycle and the guard is free to close the book — the
        # owner is warned once per window (``settle_lapsed_window``).
        _mark(db, row, "expired", AUTO_CAPPED_BY)
        log.warning("kill expand timeout: auto-expand quota used up — request %s "
                    "closed without widening (one-shot policy)", row.get("id"))
        return _res("capped", notice=_fail_notice("capped", ttl))

    triggers = list((row.get("detail") or {}).get("triggers") or [])
    if not triggers:
        # A request with no quotable limit can never widen anything: retire it
        # (with a decided_by) so it stops being a candidate every cycle.
        _mark(db, row, "expired", AUTO_DECIDED_BY)
        log.error("kill expand timeout: request %s lists no triggers — retired",
                  row.get("id"))
        return _res("retired", report=_retired_notice(ttl))

    if not breached_triggers(db, s):
        # The metrics came back inside the limits while the owner was thinking.
        # Widening now would raise a risk limit the account does not need, so
        # the request is retired and reported instead.
        _mark(db, row, "expired", AUTO_NO_BREACH_BY)
        log.info("kill expand timeout: no limit is breached any more — retired "
                 "without widening (request %s)", row.get("id"))
        return _res("no-breach", report=(
            "ℹ️ คำขอขยายลิมิตหมดอายุโดยไม่ต้องขยาย\n"
            "ไม่มีลิมิตที่เกินอยู่แล้ว (ค่ากลับมาอยู่ในกรอบ) — ลิมิตเดิมยังมีผล\n"
            "ถ้าเทรดยังหยุดอยู่ ให้พิมพ์ /resume"))

    once = plan == "once"
    decided_by = AUTO_ONCE_BY if once else AUTO_DECIDED_BY
    note = (f"⏳ ไม่มีการยืนยันภายใน {ttl:.0f} นาที — ระบบขยายลิมิตให้อัตโนมัติ\n"
            "ปรับเวลาในการรอได้ที่หน้า Settings (รูทีนนี้ทำงานทุก ~1 นาที)")
    if once:
        note = (f"⏳ ไม่มีการยืนยันภายใน {ttl:.0f} นาที — ระบบขยายลิมิตให้อัตโนมัติ "
                "1 ครั้ง\n"
                "นโยบายขยายอัตโนมัติถูกปิดอยู่: ระบบขยายให้เองได้ครั้งเดียว "
                "ครั้งต่อไปถ้ายังไม่ตอบและยังเกินลิมิตใหม่ = kill switch ปิดไม้\n"
                "ต้องการให้ขยายเองได้ทุกครั้งที่หมดเวลา → เปิดในหน้า Settings")
    reply, outcome = _approve(db, row, decided_by, note=note,
                              title=ONCE_TITLE if once else TIMEOUT_TITLE,
                              settings=s)
    if outcome == "failed":
        # The row stays OPEN so the next cycle retries; the owner is warned at
        # most once per window (the retry would otherwise repeat every minute).
        log.error("kill expand auto-apply wrote nothing (request %s)",
                  row.get("id"))
        return _res("failed", notice=_fail_notice("failed", ttl))
    if outcome == "skipped":
        log.info("kill expand timeout settled without writing (request %s)",
                 row.get("id"))
        return _res("skipped", report=reply)
    log.warning("kill expand AUTO-APPLIED (%s) after %.0f min of silence "
                "(ttl %.0f)", decided_by, age, ttl)
    return _res("applied", report=reply)


def auto_apply_expired(db, settings: Optional[AppSettings] = None
                       ) -> Optional[str]:
    """str-only view of ``settle_expired`` — the LINE report, else None.

    Kept for callers that only ask "was something widened, and what do I tell
    the owner?" (a late press, the older tests). Nothing is reported for an
    attempt that could not be settled — callers that must TELL the owner use
    ``settle_lapsed_window`` / ``settle_expired(...).notice`` instead.
    """
    return settle_expired(db, settings).report or None


def settle_lapsed_window(db, settings, notifier,
                         user_id: str = "") -> tuple[SettleResult, bool]:
    """Apply a lapsed confirmation window AND report it — one step, no skips.

    The entry point for BOTH workers that may reach the lapsed row
    (``portfolio_monitor`` and ``position_guard``), so a widening can never
    happen without the owner being told. Returns ``(result, notified)``:

    * settled (``applied``/``skipped``/``no-breach``/``retired``) → the report
      is pushed, and the owner knows exactly what happened (including "nothing
      needed writing", which must NOT be worded as a widening);
    * NOT settled and HOLDING (``failed``, see ``HOLD_KINDS``) → a warning is
      pushed at most once per ``AUTO_FAIL_NOTIFY_MIN`` (both workers retry every
      minute, and the request stays open until it can be settled); callers must
      keep the emergency exit DEFERRED rather than closing the book;
    * NOT settled and NOT holding (``capped`` — the one-shot quota is used up) →
      the same one-warning-per-window is pushed, the row is CLOSED, and callers
      may let the guard protect the account as usual.
    """
    res = settle_expired(db, settings)
    if res.settled:
        if not res.report:
            return res, False
        return res, _dispatch(notifier, user_id or DEFAULT_USER,
                              "limit_expand", res.report)
    if res.notice and _fail_notice_due(res.request_id):
        sent = _dispatch(notifier, user_id or DEFAULT_USER, "limit_expand",
                         res.notice, quick_reply_items())
        if sent:
            _note_fail_notice(res.request_id)
        return res, sent
    return res, False


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
    # settle_expired() is the one implementation; a window it could NOT settle
    # (policy off, write failing) stays open for the press below.
    settled = settle_expired(db, settings)
    if settled.settled:
        return settled.report or "ℹ️ คำขอนี้ถูกดำเนินการไปแล้ว"

    # The row is past its window and still open: the owner's press is now the
    # best chance to get it through, so it is applied to THAT row ("late").
    # Without this a late ❌ did nothing at all — and the row stayed pending,
    # ready to be widened by the timeout path the owner had just refused.
    req = pending_request(db, settings=settings)
    late = False
    if not req:
        req = stale_pending(db, settings=settings)
        late = req is not None
    if not req:
        last = latest_request(db) or {}
        if str(last.get("decided_by") or "") in AUTO_WIDEN_BY:
            return ("⏳ คำขอนี้หมดเวลายืนยันและระบบขยายลิมิตไปอัตโนมัติแล้ว\n"
                    f"• ลิมิตล่าสุดที่เขียนไป: {last.get('limit_before')}% → "
                    f"{last.get('limit_after')}%\n"
                    "ถ้าไม่ต้องการ ให้แก้ลิมิตกลับได้ที่หน้า Settings")
        if str(last.get("decided_by") or "") == AUTO_CAPPED_BY:
            # One-shot policy: this window was CLOSED without widening, so the
            # generic reply below ("ระบบจะส่งคำขอใหม่ให้อัตโนมัติ") would be a lie.
            return ("⏳ คำขอนี้หมดเวลายืนยัน และระบบไม่ขยายลิมิตให้\n"
                    "• โควตาขยายอัตโนมัติ (1 ครั้ง) ถูกใช้ไปแล้ว และนโยบายปิดอยู่\n"
                    "• ลิมิตเดิมยังมีผล — แก้ลิมิตเองที่หน้า Settings ได้\n"
                    "หรือเปิดนโยบายขยายอัตโนมัติที่หน้า Settings")
        ttl = _resolve_ttl(db, settings)
        return ("ℹ️ ไม่มีคำขอขยายลิมิตที่รอการยืนยันอยู่\n"
                "ถ้ายังเกินลิมิต ระบบจะส่งคำขอใหม่ให้อัตโนมัติ "
                f"(คำขอเดิมมีอายุ {ttl:.0f} นาที)")

    triggers = list((req.get("detail") or {}).get("triggers") or [])
    late_note = ("" if not late else
                 f"⏳ ตอบหลังหมดช่วงยืนยัน {pending_age_min(req):.0f} นาที — "
                 "คำตอบนี้ยังมีผล")
    if not approve:
        _mark(db, req, "rejected", decided_by)
        _audit(db, "limit_expand_rejected", req, triggers, approved=False)
        execution.set_pause(db, True, "risk limits kept — owner rejected expansion")
        log.info("kill expand REJECTED by %s (%s)", decided_by,
                 req.get("trigger_type"))
        note = "ลิมิตเดิมยังมีผล — เทรดยังหยุดอยู่ (ใช้ /resume ไม่ได้จนกว่าจะขยาย)"
        if late_note:
            note = f"{late_note}: ลิมิตไม่ถูกขยาย\n{note}"
        return build_limit_expand_result(
            approved=False, applied=[], remaining=triggers, kill_engaged=True,
            note=note)

    reply, _ = _approve(db, req, decided_by, note=late_note, settings=settings)
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
    # A row whose window LAPSED is normally settled by the timeout policy within
    # a minute, but when it cannot be (the settings write fails) it stays open —
    # and the emergency exit is deferred the whole time. The
    # owner must still be able to answer from the desk, so keep showing it and
    # flag it as lapsed (the LINE buttons on the original prompt are the other
    # way in). ``decide`` accepts such a press and says so in its reply.
    lapsed = False
    if req is None:
        req = stale_pending(db, s)
        lapsed = req is not None
    live = breached_triggers(db, s)         # by the monitor, never by this poll
    last = latest_request(db)
    triggers = list((req.get("detail") or {}).get("triggers") or []) if req else list(live)
    last_public = _public(last, ttl)
    if last_public and last_public["status"] == "pending":
        last_public = None              # the pending row is reported separately
    return {
        "pending": bool(req),
        "lapsed": lapsed,
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
        # What the timeout policy will do with THIS window ("ขยายอัตโนมัติ 1
        # ครั้ง", migration 039): "apply" = ทุกครั้งที่หมดเวลา, "once" =
        # ได้อีกครั้งเดียว, "capped" = หมดโควตาแล้ว (ปิดคำขอ + kill switch
        # ทำงาน). The popup reads these so its copy can never promise a
        # widening the policy will refuse.
        "auto_apply": auto_apply_enabled(s),
        "auto_apply_once": not auto_apply_enabled(s),
        "lapsed_closes": (not auto_apply_enabled(s)) and bool(req),
        "approve_command": "/dd_ok",
        "reject_command": "/dd_no",
    }


# ---------------------------------------------------------------------------
# notify (monitor calls this on every breach)
# ---------------------------------------------------------------------------
def request_and_notify(db, s: AppSettings, notifier, user_id: str = "",
                       source: str = "monitor") -> dict:
    """Settle any lapsed window, then create/push the ONE actionable prompt.

    This is the monitor's entry point on every breach. A lapsed confirmation
    window is settled here (see ``settle_lapsed_window``: the same call the
    position guard makes, so either worker may be the one that writes). That
    report is pushed first, then the cycle continues: with the limits now higher
    the breach may already be gone, in which case ``request_expand`` simply
    reports ``no_breach``. A window that could NOT be settled is reported once
    (throttled) and the ``failed`` row is retried on the next cycle.

    One-shot policy (``kill_expand_auto_apply`` False, migration 039): once the
    account has had its ONE silent widening, this stops asking for more
    (``reason="auto_capped"``, reported once). A fresh prompt would re-arm the
    guard's hold and the book would never close — the owner's decision was
    "ขยายแล้วยังไม่พอ = ปิดไม้ทันที": the silence has been answered, so the
    emergency exit must be free to act, and the way back in is Settings + /resume.
    """
    target = user_id or DEFAULT_USER
    settle, notified = settle_lapsed_window(db, s, notifier, target)
    if settle.settled:
        return {"requested": False, "reason": "auto_applied",
                "auto_applied": True, "triggers": [],
                "request": latest_request(db), "notified": notified,
                "auto_kind": settle.kind}

    if (not auto_apply_enabled(s)) and silent_widen_count(db) > 0:
        # The one-shot quota is spent (this window was `capped` or an earlier one
        # was) → do NOT stack another unanswerable prompt on the owner. The
        # warning is throttled like the other timeout notices; the settle above
        # already pushed its own message for the window that just lapsed.
        if settle.kind != "capped" and _fail_notice_due("auto_capped"):
            sent = _dispatch(notifier, target, "limit_expand",
                             build_capped_notice(), quick_reply_items())
            if sent:
                _note_fail_notice("auto_capped")
            notified = notified or sent
        return {"requested": False, "reason": "auto_capped",
                "auto_applied": False, "triggers": [],
                "request": latest_request(db), "notified": notified,
                "auto_kind": "capped"}

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
           approved: bool, decided_by: str = "") -> None:
    """risk_events row (same shape monitor writes for limit_breach).

    DB.insert swallows errors, and the authoritative audit trail is the
    kill_expand_requests row itself (status/decided_at/decided_by/detail) —
    this row is for the existing risk-event views. ``decided_by`` is copied in
    so the Logs/Audit screen can tell an OWNER approval from a timeout widening
    (``auto:expired`` / ``auto:once``) without joining the request table.
    """
    write_audit(db, event_type, {
        "request_id": req.get("id"),
        "approved": approved,
        "decided_by": decided_by or str(req.get("decided_by") or ""),
        "triggers": triggers,
        "requested_at": req.get("requested_at"),
        "limit_before": req.get("limit_before"),
        "limit_after": req.get("limit_after"),
    })


# เขียน audit ล้มเหลวเมื่อไร จำไว้ (in-process) — ให้ /api/system/risk-logs
# รู้ "ข้อเท็จจริง" แทนที่จะเดาว่า migration ยังไม่รันจากการที่ตารางว่าง
_AUDIT_FAIL: dict[str, Any] = {}
AUDIT_FAIL_TTL = 24 * 3600.0        # เกินนี้ถือว่าเก่าเกินกว่าจะเอามาเตือน
AUDIT_PROBE_EVENT = "audit_probe"   # แถวทดสอบของ db-check (insert แล้วลบทิ้ง)


def _record_audit_fail(event_type: str, err: Any) -> None:
    _AUDIT_FAIL.update({"event_type": str(event_type),
                        "error": str(err)[:400], "at": time.time()})


def audit_write_status() -> Optional[dict]:
    """ผลการเขียน audit ที่ **ล้มเหลวจริง** ครั้งล่าสุด (ภายใน 24 ชม.) หรือ None.

    WHY in-process: ไม่มีคอลัมน์ไหนเก็บ "error ของ log" ได้เอง การจำไว้ตั้งแต่
    process เริ่มทำงานจึงเป็นหลักฐานที่ตรงที่สุด และไม่โกหก — ถ้าไม่เคยล้มเหลว
    เลยตั้งแต่ start เราก็ต้องไม่บอกผู้ใช้ว่า "การเขียนไม่ลง".
    """
    if not _AUDIT_FAIL:
        return None
    age = time.time() - float(_AUDIT_FAIL.get("at") or 0)
    if age > AUDIT_FAIL_TTL:
        return None
    out = dict(_AUDIT_FAIL)
    out.pop("at", None)
    out["age_min"] = round(age / 60.0, 1)
    return out


def probe_audit(db, user_id: Optional[str] = None) -> dict:
    """insert → delete แถวทดสอบใน risk_events เพื่อพิสูจน์ทางเขียน audit.

    ใช้เส้นทางเดียวกับ write_audit เป๊ะ ๆ (insert_raw + pseudo-user 'demo')
    จึงจับได้ว่า migration 038 รันแล้วหรือยัง: ถ้า user_id ยังเป็น uuid FK
    PostgREST จะตอบ 22P02 "invalid input syntax for type uuid: \"demo\"".
    แถวทดสอบถูกลบทันที (event_type = audit_probe) จึงไม่ค้างในตารางถาวร
    """
    out: dict[str, Any] = {"table": "risk_events"}
    row = {"user_id": user_id or DEFAULT_USER, "event_type": AUDIT_PROBE_EVENT,
           "detail": {"probe": True}}
    raw: Any = getattr(db, "insert_raw", None)
    ins: Any = None
    err: Any = None
    if callable(raw):
        res = raw("risk_events", row)
        if isinstance(res, (tuple, list)) and len(res) > 1:
            ins, err = res[0], res[1]
        else:                                   # pragma: no cover
            ins = res
    else:                                       # very old fakes
        ins = db.insert("risk_events", row)
    out["insert"] = "ok" if ins else "FAIL"
    if not ins:
        out["error"] = err or "(no raw error surfaced)"
        out["hint"] = (
            "เขียน risk_events ไม่ลง. ถ้า error เป็น 'invalid input syntax for "
            "type uuid' ให้รัน database/038_risk_events_user_text.sql "
            "(user_id ต้องเป็น text)"
        )
        return out
    pid = ins.get("id") if isinstance(ins, dict) else None
    out["id"] = pid
    if pid:
        out["delete"] = "ok" if db.delete("risk_events", {"id": pid}) else "FAIL"
    return out


def write_audit(db, event_type: str, detail: dict, user_id: Optional[str] = None,
                ) -> Optional[str]:
    """เขียนแถว audit ลง risk_events แล้ว **ไม่กลืน error** — คืน error ดิบ.

    WHY ไม่ใช้ db.insert เฉย ๆ: `Database.insert` ลด error ทุกอย่างเหลือ
    log.debug บรรทัดเดียว ทำให้ prod 2026-09-14 ตรวจไม่เจอว่า audit ไม่ลงเลย
    (risk_events.user_id เป็น uuid FK แต่แอปส่ง pseudo-user 'demo' → 22P02
    "invalid input syntax for type uuid" ทุกครั้ง) หน้า Logs จึงว่างเปล่า
    โดยไม่มีใครรู้ · แก้ที่ database/038_risk_events_user_text.sql

    ล้มเหลวจะถูกจำไว้ที่ ``_AUDIT_FAIL`` (ดู ``audit_write_status``) เพื่อให้
    /api/system/risk-logs เตือนจาก "ข้อเท็จจริง" ไม่ใช่จากการเดาว่าตารางว่าง
    ใช้ risk_events เป็น "ประวัติถาวร": ไม่มี TTL และ worker ที่ purge log
    (log_maintenance) ไม่ลบตารางนี้
    """
    row = {
        "user_id": user_id or DEFAULT_USER,
        "event_type": event_type,
        "detail": detail,
    }
    raw: Any = getattr(db, "insert_raw", None)
    if not callable(raw):           # very old fakes: no raw-error surface
        try:
            db.insert("risk_events", row)
        except Exception as exc:    # pragma: no cover
            log.error("risk_events insert failed (%s): %s", event_type, exc)
            _record_audit_fail(event_type, exc)
            return str(exc)
        return None

    res = raw("risk_events", row)
    err = res[1] if isinstance(res, (tuple, list)) and len(res) > 1 else None
    if err:
        _record_audit_fail(event_type, err)
        log.error(
            "risk_events insert FAILED (%s) — เหตุการณ์นี้จะไม่ปรากฏใน audit "
            "trail: %s · ถ้าเป็น 'invalid input syntax for type uuid' ให้รัน "
            "database/038_risk_events_user_text.sql (user_id ต้องเป็น text)",
            event_type, err)
    return err
