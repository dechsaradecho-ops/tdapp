"""P1-4 — EffectiveTradingSettings: per-field value + source, fail-closed.

The audit finding: the platform could not answer "which layer supplied this
number?" without reading four files. A value could arrive from

    trading_settings DB  →  a risk PRESET  →  the legacy ENV fallback in
    core.config.Settings  →  the AppSettings schema default

and when they disagreed (the 2026-09-07 "daily loss still 2%" report) there
was no single object that named the winner.

``config_validation.resolve_effective_settings`` now resolves every
safety-relevant field to ``EffectiveField(value, source)`` and validates the
result in one call, with a fail-closed contract: an unreadable settings row
is ``readable=False`` / ``ok=False`` — never a silent default.

These tests pin the resolver, the fail-closed contract, the unsafe-config
detection (``risk_per_trade_pct > kill_daily_loss_pct``), deprecated-field
reporting and the machine-readable report the /settings/effective endpoint
and scripts/check_config.py both emit.

Run from backend/: C:\\Python314\\python.exe -m pytest tests/test_p1_4_effective_settings.py -v
"""
from __future__ import annotations

import pytest

from app.core import config_validation as cv
from app.models.schemas import AppSettings, RiskProfile, RISK_PRESETS
from tests.test_workers import FakeDatabase
from tests.test_auto_trader import db_with_client


class TestFieldSourceResolution:
    def test_stored_value_is_labelled_db(self):
        """A value that differs from the schema default came from the DB."""
        s = AppSettings(risk_per_trade_pct=2.5)   # default is 1.0
        eff = cv.resolve_effective_settings(s)
        assert eff.source_of("risk_per_trade_pct") == cv.SOURCE_DB
        assert eff.value_of("risk_per_trade_pct") == 2.5

    def test_untouched_value_is_labelled_default(self):
        """A field equal to the schema default is reported DEFAULT (honest:
        the engine uses the same number either way)."""
        s = AppSettings(capital=10_000, risk_per_trade_pct=1.0)
        eff = cv.resolve_effective_settings(s)
        # capital default is 10_000 → DEFAULT unless the preset owns it.
        assert eff.source_of("capital") == cv.SOURCE_DEFAULT

    def test_preset_owned_value_is_labelled_preset(self):
        """A field whose value equals the active profile's preset value AND is
        preset-owned is labelled preset (the owner is on a preset)."""
        mod = RISK_PRESETS[RiskProfile.moderate]
        s = AppSettings(risk_profile=RiskProfile.moderate,
                        risk_per_trade_pct=mod["risk_per_trade_pct"],
                        kill_daily_loss_pct=mod["kill_daily_loss_pct"])
        eff = cv.resolve_effective_settings(s)
        assert eff.source_of("risk_per_trade_pct") == cv.SOURCE_PRESET

    def test_all_effective_fields_are_resolved(self):
        eff = cv.resolve_effective_settings(AppSettings())
        for name in cv._EFFECTIVE_FIELDS:
            assert name in eff.fields, name
            assert eff.fields[name].source in (
                cv.SOURCE_DB, cv.SOURCE_PRESET, cv.SOURCE_DEFAULT)

    def test_value_is_never_mutated(self):
        """The resolver REPORTS; it must not change any production value."""
        s = AppSettings(risk_per_trade_pct=2.5, capital=777)
        before = s.model_dump()
        cv.resolve_effective_settings(s)
        assert s.model_dump() == before


class TestFailClosed:
    def test_none_settings_is_unknown_and_blocks(self):
        """A settings row that could NOT be read is UNKNOWN → ok False
        (fail-closed), never a silent AppSettings() substitution."""
        eff = cv.resolve_effective_settings(None)
        assert eff.readable is False
        assert eff.ok is False
        assert eff.source == cv.SOURCE_UNKNOWN
        assert eff.validation is not None
        assert eff.validation.status == cv.STATUS_UNKNOWN

    def test_unknown_source_needs_no_field_crash(self):
        eff = cv.resolve_effective_settings(None, db_ok=False)
        assert eff.source_of("risk_per_trade_pct") == cv.SOURCE_UNKNOWN
        assert eff.value_of("risk_per_trade_pct", "MISSING") == "MISSING"

    def test_readable_but_invalid_is_not_ok(self):
        """Readable but self-contradictory → ok False (still blocks)."""
        s = AppSettings(risk_per_trade_pct=5.0, kill_daily_loss_pct=2.0,
                        kill_weekly_loss_pct=10.0,
                        kill_monthly_loss_pct=20.0,
                        max_drawdown_pct=30.0)
        eff = cv.resolve_effective_settings(s)
        assert eff.readable is True
        assert eff.ok is False
        assert "per_trade_risk_exceeds_daily_loss_limit" \
            in eff.validation.error_codes

    def test_valid_settings_are_ok(self):
        s = AppSettings(capital=10_000, risk_per_trade_pct=1.0, min_lot=0.01,
                        kill_daily_loss_pct=2.0, kill_weekly_loss_pct=5.0,
                        kill_monthly_loss_pct=8.0, max_drawdown_pct=10.0)
        eff = cv.resolve_effective_settings(s)
        assert eff.readable is True and eff.ok is True


class TestUnsafeConfigDetection:
    def test_per_trade_risk_above_daily_budget_is_flagged(self):
        """The P1-4 headline unsafe example: one trade at full size can blow
        the whole day's loss budget."""
        s = AppSettings(risk_per_trade_pct=3.0, kill_daily_loss_pct=2.0,
                        kill_weekly_loss_pct=5.0, kill_monthly_loss_pct=8.0)
        eff = cv.resolve_effective_settings(s)
        assert eff.ok is False
        assert "per_trade_risk_exceeds_daily_loss_limit" \
            in eff.validation.error_codes

    def test_safe_config_has_no_errors(self):
        s = AppSettings(risk_per_trade_pct=2.0, kill_daily_loss_pct=60.0,
                        kill_weekly_loss_pct=70.0,
                        kill_monthly_loss_pct=70.0, max_drawdown_pct=15.0)
        eff = cv.resolve_effective_settings(s)
        assert eff.validation.error_codes == []
        assert eff.ok is True


class TestDeprecatedFields:
    def test_order_mode_reported_when_new_fields_empty(self):
        """A legacy row still carrying order_mode (no P1-2 fields) must have
        that field reported as DEPRECATED, not silently ignored."""
        s = AppSettings(order_mode="semi_auto", entry_mode="",
                        position_management_mode="")
        eff = cv.resolve_effective_settings(s)
        names = [d["name"] for d in eff.deprecated]
        assert "order_mode" in names

    def test_no_deprecated_when_new_fields_set(self):
        """Once the P1-2 fields are explicit, order_mode is no longer the
        derive-source and must not be flagged."""
        s = AppSettings(order_mode="semi_auto", entry_mode="confirm",
                        position_management_mode="protective_only")
        eff = cv.resolve_effective_settings(s)
        assert eff.deprecated == []

    def test_absent_order_mode_not_reported(self):
        s = AppSettings(order_mode="")
        eff = cv.resolve_effective_settings(s)
        assert eff.deprecated == []


class TestReport:
    def test_report_lines_carry_source_tags(self):
        # risk 2.3 differs from the schema default (1.0) and every preset, so
        # it is a genuine DB value.
        s = AppSettings(risk_per_trade_pct=2.3, kill_daily_loss_pct=60.0,
                        kill_weekly_loss_pct=70.0,
                        kill_monthly_loss_pct=70.0, max_drawdown_pct=15.0)
        eff = cv.resolve_effective_settings(s)
        lines = eff.report_lines()
        assert any("risk_per_trade_pct = 2.3 [DB]" in l for l in lines)

    def test_full_report_includes_validation_and_deprecated(self):
        s = AppSettings(order_mode="auto", entry_mode="", risk_per_trade_pct=3.0,
                        kill_daily_loss_pct=2.0, kill_weekly_loss_pct=5.0,
                        kill_monthly_loss_pct=8.0)
        eff = cv.resolve_effective_settings(s)
        block = "\n".join(cv.format_effective_report(eff))
        assert "=== Effective risk configuration ===" in block
        assert "=== Validation ===" in block
        assert "per_trade_risk_exceeds_daily_loss_limit" in block
        assert "=== Deprecated fields ===" in block
        assert "order_mode" in block

    def test_report_for_unreadable_settings_says_fail_closed(self):
        eff = cv.resolve_effective_settings(None)
        block = "\n".join(cv.format_effective_report(eff))
        assert "fail-closed" in block
        assert "settings_read_failed" in block

    def test_as_dict_is_json_serialisable_shape(self):
        # 2.3 is a valid value (daily 60 > 2.3) and matches no preset → DB.
        s = AppSettings(risk_per_trade_pct=2.3, kill_daily_loss_pct=60.0,
                        kill_weekly_loss_pct=70.0,
                        kill_monthly_loss_pct=70.0, max_drawdown_pct=15.0)
        eff = cv.resolve_effective_settings(s)
        d = eff.as_dict()
        assert d["readable"] is True
        assert d["ok"] is True
        assert d["fields"]["risk_per_trade_pct"]["value"] == 2.3
        assert d["fields"]["risk_per_trade_pct"]["source"] == cv.SOURCE_DB
        assert "validation" in d and "deprecated" in d


class TestEffectiveSettingsEndpoint:
    """GET /api/settings/effective — additive, read-only (P1-4)."""

    def _client(self, monkeypatch, db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import settings as settings_route

        app = FastAPI()
        app.state.db = db
        app.include_router(settings_route.router, prefix="/api/settings")
        return TestClient(app)

    def test_endpoint_returns_per_field_sources(self, monkeypatch):
        # risk 2.3 matches no preset → genuine DB value; KV client needs the
        # {id: row} dict form that try_load_settings reads.
        db = db_with_client(tables={"trading_settings": [
            {"id": 1, "risk_per_trade_pct": 2.3, "capital": 500,
             "min_lot": 0.02}]})
        db._client.store["trading_settings"] = {1: {
            "id": 1, "risk_per_trade_pct": 2.3, "capital": 500,
            "min_lot": 0.02}}
        client = self._client(monkeypatch, db)
        resp = client.get("/api/settings/effective")
        assert resp.status_code == 200
        body = resp.json()
        assert body["readable"] is True
        assert body["fields"]["risk_per_trade_pct"]["source"] == "DB"

    def test_endpoint_fail_closed_when_db_missing(self):
        """No DB → strict loader returns None → readable False (fail-closed)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import settings as settings_route

        app = FastAPI()
        app.state.db = None
        app.include_router(settings_route.router, prefix="/api/settings")
        client = TestClient(app)
        resp = client.get("/api/settings/effective")
        assert resp.status_code == 200
        body = resp.json()
        assert body["readable"] is False
        assert body["ok"] is False
