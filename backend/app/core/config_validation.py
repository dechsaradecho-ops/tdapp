"""Configuration validation for the trading settings row (P0-4).

WHY THIS EXISTS
---------------
``trading_settings`` is the single source of truth for every risk limit and
sizing parameter. A self-inconsistent row — e.g. a per-trade risk of 3% while
the daily kill-switch trips at 2% — is a configuration that can blow the
daily loss budget with a SINGLE trade before the circuit breaker ever sees
it. The platform must never silently trade on a config that contradicts
itself.

DESIGN
------
* ``validate_settings(s, *, source="db")`` is a PURE function: it reads an
  ``AppSettings`` and returns a ``ConfigValidation`` with a machine-readable
  ``status`` (VALID / INVALID / UNKNOWN) and a list of ``issues``. It never
  raises and never touches the DB.
* Fail-closed at the SEAM (``execution.execute_signal``): an INVALID config
  BLOCKS new orders with ``configuration_error`` instead of crashing a
  worker. UNKNOWN (settings could not be read at all) also blocks — a safety
  limit must never be guessed (extension of the 2026-09-22 settings-read fix).
* The Settings page, ``scripts/check_config.py`` and the /settings API all
  surface the same object, so what the owner sees is what the engine enforces.

RULES (each carries a stable, machine-readable code)
----------------------------------------------------
ERRORS — a SINGLE trade at configured size can violate a stated risk
guarantee, so an invalid config BLOCKS new orders (fail-closed):

* ``per_trade_risk_exceeds_daily_loss_limit``
      risk_per_trade_pct > kill_daily_loss_pct — one trade at full size can
      exceed the whole day's loss budget. (SPEC P0-4 headline rule.)
* ``per_trade_risk_exceeds_weekly_loss_limit``
      risk_per_trade_pct > kill_weekly_loss_pct.
* ``per_trade_risk_exceeds_monthly_loss_limit``
      risk_per_trade_pct > kill_monthly_loss_pct.
* ``per_trade_risk_exceeds_max_drawdown``
      risk_per_trade_pct > max_drawdown_pct — a single stop-out could trip the
      emergency drawdown brake.
* ``risk_per_trade_pct_non_positive``
      risk_per_trade_pct <= 0 — sizing would be zero / nonsensical.
* ``capital_non_positive``
      capital <= 0 while a positive risk is configured.
* ``min_lot_non_positive``
      min_lot <= 0 with risk sizing enabled.

WARNINGS — a config SMELL that does NOT let one trade break a guarantee, so
it is surfaced but does NOT block (the per-trade-vs-budget guard still
protects every order at execution time):

* ``kill_limit_not_ordered``
      daily > weekly or weekly > monthly (the nested loss budget windows would
      normally get LOOSER as the window widens, otherwise the tighter one is
      dead code — but this is a preference, not a safety contradiction).
* ``sl_cap_disabled_with_floor``
      a positive ``min_lot`` with ``sl_cap_enabled=False`` invites the P0-2
      breach (the floor can exceed the budget); execute_signal still BLOCKS
      that specific order, so it warns rather than blocking the whole config.

NOTE: max_drawdown_pct is deliberately NOT required to be ≤ kill_monthly_loss_pct.
They are different tiers (emergency peak-to-trough vs monthly circuit
breaker); the shipped defaults (max DD 10%, monthly 8%) are valid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.models.schemas import AppSettings


# Status values (machine-readable).
STATUS_VALID = "VALID"
STATUS_INVALID = "INVALID"
STATUS_UNKNOWN = "UNKNOWN"


@dataclass
class ConfigIssue:
    """One self-inconsistency in the settings row."""

    code: str                 # stable, machine-readable reason code
    message: str              # human-readable (Thai) explanation
    severity: str = "error"   # "error" blocks execution; "warning" does not

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "severity": self.severity}


@dataclass
class ConfigValidation:
    """Result of validating one settings snapshot."""

    status: str = STATUS_VALID
    issues: list[ConfigIssue] = field(default_factory=list)
    source: str = "db"                       # "db" | "defaults" | "unknown"
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only when VALID — INVALID and UNKNOWN both block (fail-closed)."""
        return self.status == STATUS_VALID

    @property
    def error_codes(self) -> list[str]:
        return [i.code for i in self.issues if i.severity == "error"]

    @property
    def reason(self) -> str:
        """Primary reason code for a blocked order (or '' when valid)."""
        codes = self.error_codes
        return codes[0] if codes else ""

    def summary(self) -> str:
        """One-line status for logs / LINE / API."""
        if self.status == STATUS_VALID:
            return "config VALID"
        if self.status == STATUS_UNKNOWN:
            return ("config UNKNOWN — trading_settings could not be read "
                    "(fail-closed: new orders blocked)")
        return "config INVALID: " + ", ".join(self.error_codes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "source": self.source,
            "reason": self.reason,
            "issues": [i.as_dict() for i in self.issues],
            "values": self.values,
        }


# Fields surfaced by check_config / the /settings API so the owner can see the
# EFFECTIVE numbers (and where they came from) without guessing defaults.
_EFFECTIVE_FIELDS = (
    "capital",
    "risk_per_trade_pct",
    "min_lot",
    "max_open_positions",
    "max_drawdown_pct",
    "kill_daily_loss_pct",
    "kill_weekly_loss_pct",
    "kill_monthly_loss_pct",
    "drawdown_throttle_pct",
    "sl_cap_enabled",
    "order_mode",
    "sl_distance_mode",
)


def effective_values(s: Optional[AppSettings]) -> dict[str, Any]:
    """The safety-relevant effective numbers, for display + diffing."""
    if s is None:
        return {}
    out: dict[str, Any] = {}
    for name in _EFFECTIVE_FIELDS:
        val = getattr(s, name, None)
        if isinstance(val, bool):
            val = bool(val)
        out[name] = val
    return out


def _f(s: Any, name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(s, name, default) or 0.0)
    except (TypeError, ValueError):
        return default


def validate_settings(s: Optional[AppSettings], *,
                      source: str = "db") -> ConfigValidation:
    """Validate a settings snapshot. Never raises; ``None`` → UNKNOWN.

    ``source`` documents WHERE the snapshot came from ("db" | "defaults" |
    "unknown") so the report can say "risk_per_trade_pct = 2.0 [DB]".

    ``s is None`` means the settings row could NOT be read at all — that is
    UNKNOWN and therefore blocks (fail-closed), never silently defaulted.
    """
    if s is None:
        return ConfigValidation(status=STATUS_UNKNOWN, source="unknown",
                                values={})

    issues: list[ConfigIssue] = []

    capital = _f(s, "capital")
    risk = _f(s, "risk_per_trade_pct")
    min_lot = _f(s, "min_lot")
    kill_daily = _f(s, "kill_daily_loss_pct")
    kill_weekly = _f(s, "kill_weekly_loss_pct")
    kill_monthly = _f(s, "kill_monthly_loss_pct")
    max_dd = _f(s, "max_drawdown_pct")
    sl_cap_enabled = bool(getattr(s, "sl_cap_enabled", True))

    # ---- Risk must be positive and finite --------------------------------
    if risk <= 0:
        issues.append(ConfigIssue(
            "risk_per_trade_pct_non_positive",
            f"risk_per_trade_pct = {risk:g} ต้องมากกว่า 0 "
            "(งบความเสี่ยงต่อไม้เป็น 0 = คำนวณขนาดไม้ไม่ได้)"))

    # ---- The core rule: per-trade risk must fit the daily loss budget -----
    if risk > 0 and kill_daily > 0 and risk > kill_daily:
        issues.append(ConfigIssue(
            "per_trade_risk_exceeds_daily_loss_limit",
            f"risk_per_trade_pct = {risk:g}% มากกว่า kill_daily_loss_pct = "
            f"{kill_daily:g}% — ไม้เดียวเต็มขนาดอาจทะลุเพดานขาดทุนรายวัน "
            "ก่อน circuit breaker ทำงาน"))
    if risk > 0 and kill_weekly > 0 and risk > kill_weekly:
        issues.append(ConfigIssue(
            "per_trade_risk_exceeds_weekly_loss_limit",
            f"risk_per_trade_pct = {risk:g}% มากกว่า kill_weekly_loss_pct = "
            f"{kill_weekly:g}%"))
    if risk > 0 and kill_monthly > 0 and risk > kill_monthly:
        issues.append(ConfigIssue(
            "per_trade_risk_exceeds_monthly_loss_limit",
            f"risk_per_trade_pct = {risk:g}% มากกว่า kill_monthly_loss_pct = "
            f"{kill_monthly:g}%"))
    if risk > 0 and max_dd > 0 and risk > max_dd:
        issues.append(ConfigIssue(
            "per_trade_risk_exceeds_max_drawdown",
            f"risk_per_trade_pct = {risk:g}% มากกว่า max_drawdown_pct = "
            f"{max_dd:g}% — stop-out ไม้เดียวอาจ trip เบรก drawdown ฉุกเฉิน"))

    # ---- Loss budgets must be nested (looser as the window widens) --------
    if kill_daily > 0 and kill_weekly > 0 and kill_daily > kill_weekly:
        issues.append(ConfigIssue(
            "kill_limit_not_ordered",
            f"kill_daily_loss_pct = {kill_daily:g}% มากกว่า kill_weekly_loss_pct"
            f" = {kill_weekly:g}% — เพดานต้องกว้างขึ้นเมื่อ window ยาวขึ้น",
            severity="warning"))
    if kill_weekly > 0 and kill_monthly > 0 and kill_weekly > kill_monthly:
        issues.append(ConfigIssue(
            "kill_limit_not_ordered",
            f"kill_weekly_loss_pct = {kill_weekly:g}% มากกว่า "
            f"kill_monthly_loss_pct = {kill_monthly:g}%",
            severity="warning"))

    # NOTE: max_drawdown_pct is intentionally NOT required to be ≤
    # kill_monthly_loss_pct. They are DIFFERENT tiers: the monthly circuit
    # breaker stops trading for the month, while max drawdown is the
    # peak-to-trough emergency exit. The shipped defaults (max DD 10%,
    # monthly 8%) are a deliberate, valid configuration — validating them
    # "invalid" would block every order on a healthy install.

    # ---- Capital / min_lot sanity ----------------------------------------
    if capital <= 0:
        issues.append(ConfigIssue(
            "capital_non_positive",
            f"capital = {capital:g} ต้องมากกว่า 0 เพื่อคำนวณขนาดไม้"))
    if min_lot <= 0:
        issues.append(ConfigIssue(
            "min_lot_non_positive",
            f"min_lot = {min_lot:g} ต้องมากกว่า 0"))

    # ---- SL cap vs the min-lot floor (P0-2 interaction) -------------------
    # With the cap OFF and a positive floor, the min-lot order can exceed the
    # per-trade budget and nothing re-tightens the stop (the 2026-09-11 AUDNZD
    # 6.76% vs 4% breach). That is a WARNING — execute_signal still BLOCKS the
    # over-risk order, but the config itself invites the situation.
    if min_lot > 0 and not sl_cap_enabled:
        issues.append(ConfigIssue(
            "sl_cap_disabled_with_floor",
            "min_lot > 0 แต่ sl_cap_enabled = False — ไม้เล็กสุดอาจเสี่ยงเกินงบ "
            "ต่อไม้เมื่อ SL กว้าง (ระบบจะบล็อกออเดอร์นั้นแทน ไม่เปิดเกินงบ)",
            severity="warning"))

    has_error = any(i.severity == "error" for i in issues)
    status = STATUS_INVALID if has_error else STATUS_VALID
    return ConfigValidation(status=status, issues=issues, source=source,
                            values=effective_values(s))


__all__ = [
    "ConfigIssue", "ConfigValidation", "validate_settings",
    "effective_values", "STATUS_VALID", "STATUS_INVALID", "STATUS_UNKNOWN",
]
