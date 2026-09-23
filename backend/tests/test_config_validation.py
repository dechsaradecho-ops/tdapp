"""Configuration validation (P0-4).

The settings row is the single source of truth for every risk limit. A row
that contradicts itself — e.g. a per-trade risk of 3% while the daily loss
budget trips at 2% — can blow the day with ONE trade before the circuit
breaker ever sees it. These tests pin the validation rules and, most
importantly, that ``execute_signal`` BLOCKS (never crashes) on an INVALID
config and surfaces a machine-readable ``configuration_error``.

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_config_validation.py -v
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import config_validation as cv
from app.models.schemas import AppSettings
from app.services import execution
from app.integrations.brokers import PaperBroker
from tests.test_auto_trader import db_with_client
from tests.test_workers import FakeDatabase


# --------------------------------------------------------------------------
# Pure validation
# --------------------------------------------------------------------------
class TestValidConfig:
    def test_default_settings_are_valid(self):
        r = cv.validate_settings(AppSettings())
        assert r.status == cv.STATUS_VALID
        assert r.ok is True
        assert r.error_codes == []
        assert r.reason == ""

    def test_production_like_row_is_valid(self):
        s = AppSettings(capital=10_000, risk_per_trade_pct=1.0, min_lot=0.01,
                        kill_daily_loss_pct=2.0, kill_weekly_loss_pct=5.0,
                        kill_monthly_loss_pct=8.0, max_drawdown_pct=10.0)
        r = cv.validate_settings(s)
        assert r.status == cv.STATUS_VALID
        assert r.ok is True

    def test_boundary_risk_equal_to_daily_is_valid(self):
        # risk == daily budget is allowed (not strictly greater).
        s = AppSettings(risk_per_trade_pct=2.0, kill_daily_loss_pct=2.0)
        r = cv.validate_settings(s)
        assert "per_trade_risk_exceeds_daily_loss_limit" not in r.error_codes
        assert r.ok is True

    def test_values_snapshot_is_populated(self):
        r = cv.validate_settings(AppSettings())
        assert r.values["risk_per_trade_pct"] == 2.0
        assert r.values["kill_daily_loss_pct"] == 2.0
        assert set(r.values) >= {"capital", "min_lot", "max_drawdown_pct"}


class TestInvalidConfig:
    def test_per_trade_risk_exceeds_daily_loss_limit(self):
        s = AppSettings(risk_per_trade_pct=3.0, kill_daily_loss_pct=2.0,
                        kill_weekly_loss_pct=5.0, kill_monthly_loss_pct=8.0)
        r = cv.validate_settings(s)
        assert r.status == cv.STATUS_INVALID
        assert r.ok is False
        assert "per_trade_risk_exceeds_daily_loss_limit" in r.error_codes
        assert r.reason == "per_trade_risk_exceeds_daily_loss_limit"

    def test_per_trade_risk_exceeds_weekly(self):
        s = AppSettings(risk_per_trade_pct=6.0, kill_daily_loss_pct=10.0,
                        kill_weekly_loss_pct=5.0, kill_monthly_loss_pct=20.0,
                        max_drawdown_pct=30.0)
        r = cv.validate_settings(s)
        assert "per_trade_risk_exceeds_weekly_loss_limit" in r.error_codes

    def test_per_trade_risk_exceeds_max_drawdown(self):
        s = AppSettings(risk_per_trade_pct=12.0, kill_daily_loss_pct=20.0,
                        kill_weekly_loss_pct=25.0, kill_monthly_loss_pct=30.0,
                        max_drawdown_pct=10.0)
        r = cv.validate_settings(s)
        assert "per_trade_risk_exceeds_max_drawdown" in r.error_codes

    def test_non_positive_risk(self):
        r = cv.validate_settings(AppSettings(risk_per_trade_pct=0.0))
        assert "risk_per_trade_pct_non_positive" in r.error_codes
        assert r.ok is False

    def test_kill_limits_must_be_nested(self):
        # A config smell → WARNING, not an error (does not block all orders).
        s = AppSettings(risk_per_trade_pct=1.0, kill_daily_loss_pct=8.0,
                        kill_weekly_loss_pct=5.0, kill_monthly_loss_pct=10.0,
                        max_drawdown_pct=20.0)
        r = cv.validate_settings(s)
        assert "kill_limit_not_ordered" in [i.code for i in r.issues]
        assert "kill_limit_not_ordered" not in r.error_codes
        assert r.ok is True

    def test_default_max_drawdown_wider_than_monthly_is_valid(self):
        # Shipped defaults: max DD 10% while monthly breaker is 8%. They are
        # DIFFERENT tiers (emergency peak-to-trough vs monthly circuit
        # breaker) and this is a deliberately valid configuration.
        s = AppSettings(risk_per_trade_pct=1.0, kill_daily_loss_pct=2.0,
                        kill_weekly_loss_pct=5.0, kill_monthly_loss_pct=8.0,
                        max_drawdown_pct=10.0)
        r = cv.validate_settings(s)
        assert r.ok is True

    def test_non_positive_capital(self):
        r = cv.validate_settings(AppSettings(capital=0.0))
        assert "capital_non_positive" in r.error_codes

    def test_non_positive_min_lot(self):
        r = cv.validate_settings(AppSettings(min_lot=0.0))
        assert "min_lot_non_positive" in r.error_codes


class TestWarnings:
    def test_sl_cap_disabled_with_floor_is_a_warning_not_an_error(self):
        s = AppSettings(min_lot=0.02, sl_cap_enabled=False)
        r = cv.validate_settings(s)
        assert "sl_cap_disabled_with_floor" in [i.code for i in r.issues]
        assert r.ok is True                       # warning does not block
        assert "sl_cap_disabled_with_floor" not in r.error_codes


class TestUnknown:
    def test_none_settings_is_unknown_and_blocks(self):
        r = cv.validate_settings(None)
        assert r.status == cv.STATUS_UNKNOWN
        assert r.ok is False
        assert r.source == "unknown"
        assert "could not be read" in r.summary()

    def test_summary_strings(self):
        assert "VALID" in cv.validate_settings(AppSettings()).summary()
        bad = cv.validate_settings(AppSettings(risk_per_trade_pct=9.0))
        assert bad.summary().startswith("config INVALID")

    def test_as_dict_shape(self):
        d = cv.validate_settings(AppSettings(risk_per_trade_pct=9.0)).as_dict()
        assert d["status"] == cv.STATUS_INVALID
        assert d["ok"] is False
        assert d["reason"]
        assert isinstance(d["issues"], list) and d["issues"]


# --------------------------------------------------------------------------
# execute_signal seam (fail-closed, never crashes)
# --------------------------------------------------------------------------
class _RecordingNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, user_id, ntype, message, critical=None,
                     quick_reply=None):
        self.sent.append({"type": ntype, "message": message})


class _RecordingBroker(PaperBroker):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.orders = []

    async def place_order(self, req):
        self.orders.append(req)
        return await super().place_order(req)


class TestExecuteSignalBlocksInvalidConfig:
    def test_invalid_config_blocks_before_any_gate(self):
        db = FakeDatabase()
        broker = _RecordingBroker()
        notifier = _RecordingNotifier()
        bad = AppSettings(risk_per_trade_pct=3.0, kill_daily_loss_pct=2.0,
                          kill_weekly_loss_pct=5.0, kill_monthly_loss_pct=8.0)
        report = asyncio.run(execution.execute_signal(
            db, broker, notifier, bad,
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.1000, stop_loss=1.0950, take_profit=1.1100,
            confidence=90.0, opportunity=90.0, signal_id="sig-1",
            source="auto"))
        assert report.allowed is False
        assert report.size_lots == 0.0
        assert report.configuration_error == "per_trade_risk_exceeds_daily_loss_limit"
        assert any("configuration_error" in r for r in report.rejects)
        # The order MUST NOT have reached the broker.
        assert broker.orders == []

    def test_valid_config_is_not_blocked_by_validation(self):
        db = FakeDatabase()
        broker = _RecordingBroker()
        notifier = _RecordingNotifier()
        good = AppSettings(capital=10_000, risk_per_trade_pct=1.0)
        report = asyncio.run(execution.execute_signal(
            db, broker, notifier, good,
            user_id="demo", asset="EURUSD", direction="BUY",
            entry=1.1000, stop_loss=1.0950, take_profit=1.1100,
            confidence=90.0, opportunity=90.0, signal_id="sig-1",
            source="auto"))
        # Validation did NOT block (other gates may or may not, but the
        # configuration_error field is the discriminator we assert here).
        assert report.configuration_error is None
