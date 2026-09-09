"""Risk-default parity lock — 0.5 / 2 / 5 / 8 / 10.

Three layers must agree so a breach pauses with the same numbers everywhere:
  1. RiskConfig field defaults (engine-level single truth)
  2. config.py env defaults (fallback only — live code reads the DB row)
  3. RiskConfig.from_app_settings(None-field) fallbacks

Plus the usage rule: production paths must go through
risk_engine_for_settings(s) (DB row), never bare RiskEngine() (ENV).

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_risk_defaults.py -v
"""
from __future__ import annotations


def test_risk_config_field_defaults_are_spec():
    from app.engine.risk_engine import (
        DEFAULT_MAX_DAILY_LOSS_PCT,
        DEFAULT_MAX_DRAWDOWN_PCT,
        DEFAULT_MAX_MONTHLY_LOSS_PCT,
        DEFAULT_MAX_WEEKLY_LOSS_PCT,
        DEFAULT_RISK_PER_TRADE_PCT,
        RiskConfig,
    )
    assert (DEFAULT_RISK_PER_TRADE_PCT, DEFAULT_MAX_DAILY_LOSS_PCT,
            DEFAULT_MAX_WEEKLY_LOSS_PCT, DEFAULT_MAX_MONTHLY_LOSS_PCT,
            DEFAULT_MAX_DRAWDOWN_PCT) == (0.5, 2.0, 5.0, 8.0, 10.0)
    cfg = RiskConfig()
    assert (cfg.risk_per_trade_pct, cfg.max_daily_loss_pct,
            cfg.max_weekly_loss_pct, cfg.max_monthly_loss_pct,
            cfg.max_drawdown_pct) == (0.5, 2.0, 5.0, 8.0, 10.0)


def test_env_defaults_match_spec():
    from app.core.config import Settings
    s = Settings()
    assert (s.default_risk_per_trade, s.default_max_daily_loss,
            s.default_max_weekly_loss, s.default_max_monthly_loss,
            s.default_max_drawdown) == (0.5, 2.0, 5.0, 8.0, 10.0)


def test_from_app_settings_none_falls_back_to_spec():
    """A legacy row with NULL risk columns must behave like spec defaults —
    never 0/None (which would disable the gate) and never AppSettings' own
    user-facing default (risk_per_trade 1.0 is the moderate profile, not
    the engine fallback)."""
    from app.engine.risk_engine import RiskConfig
    from app.models.schemas import AppSettings
    # model_construct bypasses validation: AppSettings risk fields are
    # non-Optional floats, but a legacy DB row can still carry NULLs
    # (filtered by _row_to_settings) — from_app_settings must survive that.
    s = AppSettings.model_construct(risk_per_trade_pct=None, kill_daily_loss_pct=None,
                    kill_weekly_loss_pct=None, kill_monthly_loss_pct=None,
                    max_drawdown_pct=None)
    cfg = RiskConfig.from_app_settings(s)
    assert cfg.risk_per_trade_pct == 0.5
    assert cfg.max_daily_loss_pct == 2.0
    assert cfg.max_weekly_loss_pct == 5.0
    assert cfg.max_monthly_loss_pct == 8.0
    assert cfg.max_drawdown_pct == 10.0


def test_production_paths_use_settings_row_not_env(monkeypatch):
    """portfolio_monitor + /risk/check + chat risk block must route through
    risk_engine_for_settings (DB row). Bare RiskEngine() in those paths
    would silently evaluate against ENV defaults instead of the user's
    Settings page values (the 2026-09-07 5%-vs-2% bug)."""
    import inspect
    import app.workers.portfolio_monitor as pm
    import app.api.routes.risk as risk_route
    import app.api.routes.chat as chat_route
    import app.services.execution as ex

    helper = "risk_engine_for_settings"
    for mod, label in ((pm, "portfolio_monitor"), (risk_route, "risk route"),
                       (chat_route, "chat route"), (ex, "execution monitor")):
        src = inspect.getsource(mod)
        assert helper in src, f"{label} must use {helper}"
        # strip comments/docstrings: "RiskEngine()" may appear in prose
        # (e.g. portfolio_monitor documents why bare construction is wrong)
        code_lines = [ln for ln in src.splitlines()
                      if not ln.strip().startswith(("#", '"""', "'''"))]
        code = "\n".join(code_lines)
        assert "RiskEngine(" not in code.replace(helper, "").replace(
            "RiskConfig", ""), f"{label} must not construct bare RiskEngine()"


def test_breach_pauses_and_reports_db_row_limits():
    """End-to-end at spec defaults: a 2.5% daily loss pauses, the message
    names the DB-row limit, and the pause reason carries it (LINE alert +
    settings banner show the same number)."""
    from app.engine.risk_engine import (
        PortfolioSnapshot, risk_engine_for_settings,
    )
    from app.models.schemas import AppSettings
    engine = risk_engine_for_settings(AppSettings())
    snap = PortfolioSnapshot(
        starting_capital=10_000.0, peak_equity=10_000.0,
        current_equity=9_750.0, realized_pnl_today=-250.0,
        realized_pnl_week=0.0, realized_pnl_month=0.0, open_risk=0.0)
    status = engine.check(snap)
    assert status.trading_paused is True
    assert status.daily_loss_limit == 2.0
    assert "2.00%" in status.message

    healthy = engine.check(PortfolioSnapshot(
        starting_capital=10_000.0, peak_equity=10_000.0,
        current_equity=10_000.0, realized_pnl_today=0.0,
        realized_pnl_week=0.0, realized_pnl_month=0.0, open_risk=0.0))
    assert healthy.trading_paused is False
