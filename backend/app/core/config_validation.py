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


# =========================================================================
# P1-4 — EffectiveTradingSettings: per-field VALUE + SOURCE
# =========================================================================
#
# BEFORE P1-4 there was no single place that could answer "where did this
# number come from?". ``trading_settings`` (DB) is the runtime source of
# truth, but a value could ALSO arrive from a risk PRESET, from the legacy
# ENV fallback in ``core.config.Settings`` (default_risk_per_trade etc.), or
# from the schema DEFAULT compiled into ``AppSettings``. When they disagreed
# (the 2026-09-07 "daily loss limit still 2%" report) there was no way to
# tell WHICH layer won without reading four files.
#
# ``EffectiveTradingSettings`` resolves every safety-relevant field to one
# ``EffectiveField(value, source)`` so ``check_config.py`` (and the /settings
# API) can print exactly what the engine will use and why.
#
# SOURCE PRECEDENCE (highest first):
#   PRESET   the stored value equals the risk_profile's preset value and the
#            field is preset-owned (RISK_PRESET_FIELDS) — i.e. the profile is
#            governing this field (indistinguishable from a manual entry set
#            to the same number; the EFFECTIVE value is identical either way)
#   DB       the trading_settings row (id=1) supplied a value that differs
#            from both the preset and the schema default
#   DEFAULT  neither preset nor DB — the schema default in AppSettings applies
#   UNKNOWN  the settings row could NOT be read (fail-closed; value is None)
#
# NOTE the ENV fallback in ``core.config.Settings`` is documented per field as
# ``env_default`` (a DIFFERENT layer from the schema default); it is the
# fallback for the RiskEngine when a field is missing, never the live limit.

SOURCE_DB = "DB"
SOURCE_PRESET = "preset"
SOURCE_FALLBACK = "fallback"
SOURCE_DEFAULT = "default"
SOURCE_UNKNOWN = "unknown"

#: Sources that are safe to trade on (a real value was resolved).
_RESOLVED_SOURCES = (SOURCE_DB, SOURCE_PRESET, SOURCE_FALLBACK, SOURCE_DEFAULT)


@dataclass
class EffectiveField:
    """One resolved setting: its effective value and where it came from."""

    name: str
    value: Any
    source: str = SOURCE_DEFAULT
    schema_default: Any = None
    env_default: Any = None      # legacy core.config.Settings fallback (or None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "source": self.source,
            "schema_default": self.schema_default,
            "env_default": self.env_default,
        }


def _same(a: Any, b: Any) -> bool:
    """Loose equality that treats numeric 2 and 2.0 as equal, None==None."""
    if a is None or b is None:
        return a is b
    try:
        return abs(float(a) - float(b)) <= 1e-9
    except (TypeError, ValueError):
        return a == b


#: Legacy ENV fallback field map — ``AppSettings`` field → ``core.config``
#: Settings attribute. Only the risk defaults have an ENV layer.
_ENV_FALLBACK_FIELDS: dict[str, str] = {
    "risk_per_trade_pct": "default_risk_per_trade",
    "kill_daily_loss_pct": "default_max_daily_loss",
    "kill_weekly_loss_pct": "default_max_weekly_loss",
    "kill_monthly_loss_pct": "default_max_monthly_loss",
    "max_drawdown_pct": "default_max_drawdown",
}


@dataclass
class EffectiveTradingSettings:
    """Every safety-relevant setting resolved to ``(value, source)`` (P1-4).

    Built by :func:`resolve_effective_settings`. ``settings`` is the underlying
    ``AppSettings`` (or ``None`` when the DB row could not be read). The
    ``validation`` carries the same status/ok fail-closed contract as
    ``validate_settings`` so callers can do BOTH "what value" and "is it safe"
    from one object.
    """

    settings: Optional[AppSettings] = None
    fields: dict[str, EffectiveField] = field(default_factory=dict)
    source: str = SOURCE_UNKNOWN
    validation: Optional["ConfigValidation"] = None
    deprecated: list[dict[str, Any]] = field(default_factory=list)

    # -- fail-closed contract --------------------------------------------
    @property
    def readable(self) -> bool:
        """False when the settings row could NOT be read (fail-closed)."""
        return self.settings is not None

    @property
    def ok(self) -> bool:
        """True only when readable AND validated VALID — either failure blocks."""
        return self.readable and (self.validation is not None
                                  and self.validation.ok)

    def value_of(self, name: str, default: Any = None) -> Any:
        f = self.fields.get(name)
        return default if f is None else f.value

    def source_of(self, name: str) -> str:
        f = self.fields.get(name)
        return SOURCE_UNKNOWN if f is None else f.source

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "readable": self.readable,
            "ok": self.ok,
            "validation": self.validation.as_dict() if self.validation else None,
            "fields": {n: f.as_dict() for n, f in self.fields.items()},
            "deprecated": list(self.deprecated),
        }

    def report_lines(self) -> list[str]:
        """Human-readable ``name = value [SOURCE]`` lines (for check_config)."""
        lines: list[str] = []
        for name in _EFFECTIVE_FIELDS:
            f = self.fields.get(name)
            if f is None:
                continue
            tag = f.source.upper() if f.source != SOURCE_UNKNOWN else "UNKNOWN"
            suffix = ""
            if f.source == SOURCE_DEFAULT and f.env_default is not None \
                    and not _same(f.value, f.env_default):
                suffix = f"  (ENV fallback = {f.env_default})"
            lines.append(f"{name} = {f.value} [{tag}]{suffix}")
        return lines


def _deprecated_fields(s: AppSettings) -> list[dict[str, Any]]:
    """Legacy/derived fields the owner may still have set (P1-4).

    ``order_mode`` is the P1-2 legacy axis: it is still read to DERIVE
    ``entry_mode`` / ``position_management_mode`` when those are empty, so a
    row carrying it is not wrong — but it is superseded and the report should
    say so instead of letting the two silently disagree.
    """
    out: list[dict[str, Any]] = []
    entry = str(getattr(s, "entry_mode", "") or "").strip()
    mgmt = str(getattr(s, "position_management_mode", "") or "").strip()
    legacy = str(getattr(s, "order_mode", "") or "").strip()
    if legacy and not (entry and mgmt):
        out.append({
            "name": "order_mode",
            "value": legacy,
            "reason": "superseded by entry_mode / position_management_mode "
                      "(P1-2) — still read as the derive-fallback when the "
                      "new fields are empty",
        })
    return out


def resolve_effective_settings(
    settings: Optional[AppSettings],
    *,
    db_ok: bool = True,
    source: Optional[str] = None,
    env_settings: Any = None,
) -> EffectiveTradingSettings:
    """Resolve per-field value + source and validate (P1-4).

    ``settings`` is the DB row (``AppSettings``) or ``None`` when the read
    FAILED. ``db_ok`` says whether the DB layer is reachable at all — a
    ``None`` settings with ``db_ok=False`` is an UNKNOWN read (fail-closed),
    the same contract as ``validate_settings(None)``.

    Never raises. Never mutates the values — this is a REPORTING/verification
    layer; it does not change any production setting.
    """
    # Read failure → fail-closed UNKNOWN (no silent default substitution).
    if settings is None:
        validation = validate_settings(None, source="unknown")
        eff_source = SOURCE_UNKNOWN
        if db_ok:
            eff_source = SOURCE_UNKNOWN  # row missing is also UNKNOWN here
        return EffectiveTradingSettings(
            settings=None, fields={}, source=eff_source,
            validation=validation, deprecated=[])

    eff_source = source or SOURCE_DB
    validation = validate_settings(settings, source=eff_source)

    # Resolve the legacy ENV layer lazily (only for the risk-default fields).
    if env_settings is None:
        try:                                    # pragma: no cover - env
            from app.core.config import get_settings as _get_env_settings
            env_settings = _get_env_settings()
        except Exception:                       # pragma: no cover - env
            env_settings = None

    profile = getattr(settings, "risk_profile", None)
    preset: dict[str, Any] = {}
    try:
        from app.models.schemas import RISK_PRESETS, RiskProfile
        p = profile if isinstance(profile, RiskProfile) else RiskProfile(
            str(profile or RiskProfile.moderate.value))
        preset = dict(RISK_PRESETS.get(p, {}))
    except Exception:                           # pragma: no cover - defensive
        preset = {}

    fields: dict[str, EffectiveField] = {}
    for name in _EFFECTIVE_FIELDS:
        if not hasattr(AppSettings, "model_fields") or \
                name not in AppSettings.model_fields:
            continue
        schema_default = AppSettings.model_fields[name].default
        value = getattr(settings, name, schema_default)
        env_name = _ENV_FALLBACK_FIELDS.get(name)
        env_default = getattr(env_settings, env_name, None) if env_name else None

        # DB wins whenever the stored value differs from the schema default.
        if name in preset and _same(value, preset.get(name)):
            # PRESET-owned field sitting exactly at the active profile's
            # preset value → report PRESET (the profile governs this field).
            # NOTE: this is indistinguishable from a manual entry that
            # happens to equal the preset — either way the EFFECTIVE value is
            # identical, so the label states the profile that owns it.
            fsource = SOURCE_PRESET
        elif not _same(value, schema_default):
            fsource = SOURCE_DB
        else:
            fsource = SOURCE_DEFAULT

        fields[name] = EffectiveField(
            name=name, value=value, source=fsource,
            schema_default=schema_default, env_default=env_default)

    return EffectiveTradingSettings(
        settings=settings, fields=fields, source=eff_source,
        validation=validation, deprecated=_deprecated_fields(settings))


def format_effective_report(eff: EffectiveTradingSettings) -> list[str]:
    """Full P1-4 report block (effective values + validation + deprecated)."""
    lines: list[str] = ["=== Effective risk configuration ==="]
    if not eff.readable:
        lines.append("(trading_settings could not be read — fail-closed: "
                     "new orders blocked)")
        lines.append("")
        lines.append("=== Validation ===")
        lines.append("source = unknown")
        lines.append(f"status = {eff.validation.status if eff.validation else STATUS_UNKNOWN}")
        lines.append("reason = settings_read_failed")
        return lines
    lines.extend(eff.report_lines())
    lines.append("")
    lines.append("=== Validation ===")
    lines.append(f"source = {eff.source}")
    if eff.validation:
        lines.append(f"status = {eff.validation.status}")
        for issue in eff.validation.issues:
            lines.append(f"  [{issue.severity.upper()}] {issue.code}: "
                         f"{issue.message}")
        lines.append(f"reason = {eff.validation.reason or '-'}")
    if eff.deprecated:
        lines.append("")
        lines.append("=== Deprecated fields ===")
        for d in eff.deprecated:
            lines.append(f"  {d['name']} = {d['value']} — {d['reason']}")
    return lines


__all__ = [
    "ConfigIssue", "ConfigValidation", "validate_settings",
    "effective_values", "STATUS_VALID", "STATUS_INVALID", "STATUS_UNKNOWN",
    "EffectiveField", "EffectiveTradingSettings", "resolve_effective_settings",
    "format_effective_report", "SOURCE_DB", "SOURCE_PRESET", "SOURCE_FALLBACK",
    "SOURCE_DEFAULT", "SOURCE_UNKNOWN",
]
