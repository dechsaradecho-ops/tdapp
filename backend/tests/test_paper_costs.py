"""Tests for the paper-trading exit-side cost model (audit item 7).

Before migration 033 the simulation charged a spread on the ENTRY only
(`apply_spread` in execute_signal) and nothing on the way out, so paper PnL
was systematically optimistic: of the 7 trades prod had closed, only 2 had
lost more than a quarter of a single spread, and a trailed stop booked +10.5.

The cost is deducted from the REALIZED number — the recorded exit price is
never touched, so the journal stays comparable with the chart.

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_paper_costs.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.schemas import AppSettings
from app.services.execution import PaperBrokerPnl, paper_exit_cost


def _pos(direction="BUY", entry=1.0850, price=1.0950, volume=0.01, asset="EURUSD"):
    return SimpleNamespace(direction=direction, entry_price=entry,
                           current_price=price, volume=volume, asset=asset)


class TestPaperExitCost:
    def test_eurusd_round_trip_components(self):
        """0.5 × EURUSD spread 0.00010 × 1 lot × 100k = 5.00 spread
        + commission 3.5 × 2 sides = 7.00 → 12.00 per standard lot."""
        assert paper_exit_cost(AppSettings(), "EURUSD", 1.0) == pytest.approx(12.0)

    def test_xauusd_uses_gold_contract_size(self):
        """Gold: 0.5 × 0.30 × 1 lot × 100 oz = 15.00 + 7.00 commission."""
        assert paper_exit_cost(AppSettings(), "XAUUSD", 1.0) == pytest.approx(22.0)

    def test_scales_linearly_with_volume(self):
        assert paper_exit_cost(AppSettings(), "XAUUSD", 0.01) == pytest.approx(0.22)

    def test_zero_volume_is_free(self):
        assert paper_exit_cost(AppSettings(), "EURUSD", 0.0) == 0.0

    def test_disabled_settings_return_zero(self):
        """Both knobs at 0 restores the pre-2026-09-11 (cost-free) model."""
        s = AppSettings(paper_exit_spread_mult=0.0, paper_commission_per_lot=0.0)
        assert paper_exit_cost(s, "XAUUSD", 1.0) == 0.0

    def test_commission_only(self):
        s = AppSettings(paper_exit_spread_mult=0.0, paper_commission_per_lot=3.5)
        assert paper_exit_cost(s, "EURUSD", 2.0) == pytest.approx(14.0)

    def test_exit_spread_only(self):
        s = AppSettings(paper_exit_spread_mult=1.0, paper_commission_per_lot=0.0)
        assert paper_exit_cost(s, "EURUSD", 1.0) == pytest.approx(10.0)

    def test_spread_override_is_honoured(self):
        s = AppSettings(spread_overrides={"EURUSD": 0.0010},
                        paper_commission_per_lot=0.0)
        assert paper_exit_cost(s, "EURUSD", 1.0) == pytest.approx(50.0)

    def test_unknown_asset_without_spread_is_commission_only(self):
        s = AppSettings(paper_commission_per_lot=3.5)  # paper_spread defaults to 0
        assert paper_exit_cost(s, "ZZZUSD", 1.0) == pytest.approx(7.0)

    def test_none_settings_and_broken_values_never_raise(self):
        assert paper_exit_cost(None, "EURUSD", 1.0) == 0.0
        assert paper_exit_cost(AppSettings(), "EURUSD", "abc") == 0.0

        class Broken:
            paper_exit_spread_mult = "oops"
            paper_commission_per_lot = None
        assert paper_exit_cost(Broken(), "EURUSD", 1.0) == 0.0


class TestPaperBrokerPnlWithCosts:
    def test_gross_unchanged_without_settings(self):
        """Display-only callers (unrealized equity) keep the old behaviour."""
        assert PaperBrokerPnl.compute(_pos()) == pytest.approx(10.0)

    def test_realized_pnl_deducts_commission_and_exit_spread(self):
        # 0.01 EURUSD lot, +100 pips → +10.00 gross − 0.12 cost
        assert PaperBrokerPnl.compute(_pos(), AppSettings()) == pytest.approx(9.88)

    def test_loss_grows_by_the_cost(self):
        pos = _pos(entry=1.0850, price=1.0800)  # −50 pips → −5.00 gross
        assert PaperBrokerPnl.compute(pos, AppSettings()) == pytest.approx(-5.12)

    def test_flat_trade_is_not_free(self):
        """Exit == entry must not report a free round trip."""
        pos = _pos(entry=1.0850, price=1.0850)
        assert PaperBrokerPnl.compute(pos, AppSettings()) == pytest.approx(-0.12)

    def test_gold_one_lot_sl_hit(self):
        pos = _pos(direction="BUY", entry=100.0, price=94.0, volume=1.0,
                   asset="XAUUSD")
        assert PaperBrokerPnl.compute(pos, AppSettings(), asset="XAUUSD") \
            == pytest.approx(-622.0)  # −600 gross − 22 cost

    def test_asset_falls_back_to_position_attribute(self):
        pos = _pos(volume=1.0, price=94.0, entry=100.0, asset="XAUUSD")
        assert PaperBrokerPnl.compute(pos, AppSettings()) == pytest.approx(-622.0)

    def test_sell_direction_sign(self):
        pos = _pos(direction="SELL", entry=100.0, price=89.0, volume=1.0,
                   asset="XAUUSD")
        assert PaperBrokerPnl.compute(pos, AppSettings(), asset="XAUUSD") \
            == pytest.approx(1078.0)  # +1100 gross − 22 cost
