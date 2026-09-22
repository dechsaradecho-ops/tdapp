r"""Print the EFFECTIVE risk configuration and validate it (P0-4).

Shows, for every safety-relevant number, the value the engine will actually
use and WHERE it came from:

    risk_per_trade_pct = 2.0 [DB]
    kill_daily_loss_pct = 2.0 [DB]
    ...
    status = INVALID
    reason = per_trade_risk_exceeds_daily_loss_limit

Source legend:
    [DB]       the trading_settings row (id=1) supplied this value
    [DEFAULT]  the row was missing/None for this field → schema default
    [UNKNOWN]  the settings row could not be read at all (fail-closed)

Exit code: 0 = VALID, 2 = INVALID, 3 = UNKNOWN — so CI / cron can gate on it.

Run:  d:/tdapp/.venv/Scripts/python.exe scripts/check_config.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ lives one level under backend/ — make `app` importable both when
# run as `python scripts/check_config.py` and from inside backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings          # noqa: E402
from app.core import config_validation            # noqa: E402
from app.models.schemas import AppSettings        # noqa: E402


def _safe_reconfigure() -> None:
    """Thai output on a cp1252 Windows console must not crash the script."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_effective_settings():
    """Read the trading_settings row the same way the engine does.

    Returns ``(settings_or_None, source)`` where source is "db", "defaults"
    or "unknown" (mirrors app/api/routes/settings.try_load_settings).
    """
    try:
        from app.services.database import Database
        db = Database()
    except Exception as exc:                       # pragma: no cover - env
        print(f"DB init failed: {exc}")
        return None, "unknown"
    if not db or not db.available:
        print("DB unavailable — cannot read trading_settings")
        return None, "unknown"
    try:
        from app.api.routes.settings import try_load_settings
        s = try_load_settings(db)
        if s is None:
            return None, "unknown"                 # row could NOT be read
        return s, "db"
    except Exception as exc:                        # pragma: no cover - env
        print(f"settings read failed: {exc}")
        return None, "unknown"


def _field_source(s: AppSettings, name: str) -> str:
    """Deprecated shim — P1-4 resolves the source in ``config_validation``.

    Kept because some operators' muscle memory greps this symbol; it now
    delegates to the single resolver so the label can never drift from the
    per-field source printed by ``format_effective_report``.
    """
    eff = config_validation.resolve_effective_settings(s)
    return f"[{eff.source_of(name).upper()}]"


def main() -> int:
    _safe_reconfigure()
    settings = get_settings()

    print("=== Environment ===")
    print(f"SUPABASE_URL: {settings.supabase_url}")
    print(f"SUPABASE_PUBLISHABLE_KEY: "
          f"{settings.supabase_publishable_key[:20]}..." if settings.supabase_publishable_key
          else "SUPABASE_PUBLISHABLE_KEY: Not set")
    print(f"APP_ENV: {settings.app_env}")
    print(f"effective service key: "
          f"{settings.effective_service_key[:20]}..." if settings.effective_service_key
          else "effective service key: Not set")
    print()

    s, source = _load_effective_settings()
    # P1-4: one resolver yields per-field value + source + validation +
    # deprecated fields, so what is printed is exactly what the engine uses.
    eff = config_validation.resolve_effective_settings(s, source=source)
    result = eff.validation or config_validation.validate_settings(
        s, source=source)

    for line in config_validation.format_effective_report(eff):
        print(line)
    print()

    if result.status == config_validation.STATUS_VALID:
        print("OK — configuration is self-consistent.")
        return 0
    if result.status == config_validation.STATUS_INVALID:
        print("INVALID — new orders will be BLOCKED until the config is fixed "
              "(configuration_error).")
        return 2
    print("UNKNOWN — trading_settings could not be read (fail-closed: new "
          "orders blocked).")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
