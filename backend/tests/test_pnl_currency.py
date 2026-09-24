"""PnL currency conversion tests (2026-09-22).

Prod bug PAPER-000083: a USDJPY SELL 0.02 lots, entry 157.024 → exit 157.013
was journaled as ``pnl = 22.0`` — but ``sign × price_diff × lots × contract``
is in the QUOTE currency, so the real figure is ¥22 ≈ **$0.14**, not $22
(≈157× overstatement). The bug is invisible on XXXUSD pairs (EURUSD, GBPUSD,
XAUUSD) whose quote currency already IS the account currency.

Everything below is asserted in the ACCOUNT currency (USD).
"""

from types import SimpleNamespace

import pytest

from app.models.schemas import (
    ACCOUNT_CURRENCY,
    asset_currencies,
    base_currency,
    convert_pnl_to_account,
    pnl_conversion_asset,
    quote_currency,
)
from app.services.execution import (
    PaperBrokerPnl,
    fetch_pnl_rates,
    pnl_breakdown,
    pnl_conversion_rate,
)


def _settings(**over):
    """AppSettings-like object with cost fields defaulted to zero."""
    base = dict(
        paper_exit_spread_mult=0.0,
        paper_commission_per_lot=0.0,
        paper_spread=0.0,
        spread_overrides={},
    )
    base.update(over)
    return SimpleNamespace(**base)


def _pos(asset, direction, entry, mark, volume):
    return SimpleNamespace(
        direction=direction, asset=asset,
        entry_price=entry, current_price=mark, volume=volume)


# ---------------------------------------------------------------------------
# Currency introspection
# ---------------------------------------------------------------------------
class TestAssetCurrencies:
    @pytest.mark.parametrize("asset,expected", [
        ("USDJPY", ("USD", "JPY")),
        ("EURUSD", ("EUR", "USD")),
        ("GBPJPY", ("GBP", "JPY")),
        ("EURJPY", ("EUR", "JPY")),
        ("XAUUSD", ("XAU", "USD")),
        ("XAGUSD", ("XAG", "USD")),
    ])
    def test_asset_currencies(self, asset, expected):
        assert asset_currencies(asset) == expected

    def test_quote_and_base(self):
        assert quote_currency("USDJPY") == "JPY"
        assert base_currency("USDJPY") == "USD"
        assert quote_currency("EURUSD") == "USD"
        assert base_currency("EURUSD") == "EUR"

    def test_unknown_asset_is_none(self):
        assert quote_currency("") is None
        assert base_currency("TOOSHORT") is None

    def test_pnl_conversion_asset(self):
        # USD-quote legs need no conversion symbol
        assert pnl_conversion_asset("EURUSD") is None
        assert pnl_conversion_asset("XAUUSD") is None
        # non-USD quote → the <quote>USD leg
        assert pnl_conversion_asset("USDJPY") == "JPYUSD"
        assert pnl_conversion_asset("GBPJPY") == "JPYUSD"

    def test_account_currency_is_usd(self):
        assert ACCOUNT_CURRENCY == "USD"


# ---------------------------------------------------------------------------
# convert_pnl_to_account — fail-closed contract
# ---------------------------------------------------------------------------
class TestConvertPnlToAccount:
    def test_usd_quote_passes_through(self):
        assert convert_pnl_to_account(123.45, "EURUSD", None) == 123.45
        assert convert_pnl_to_account(1000.0, "XAUUSD", None) == 1000.0

    def test_none_amount_stays_none(self):
        assert convert_pnl_to_account(None, "USDJPY", 0.0063) is None

    def test_non_usd_quote_divides_by_rate(self):
        # ¥22 at 1/157.013 per yen → ~$0.1401
        assert convert_pnl_to_account(22.0, "USDJPY", 1 / 157.013) == pytest.approx(0.1401, abs=1e-4)

    def test_missing_rate_fails_closed(self):
        assert convert_pnl_to_account(22.0, "USDJPY", None) is None

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
    def test_non_positive_rate_fails_closed(self, bad):
        assert convert_pnl_to_account(22.0, "USDJPY", bad) is None


# ---------------------------------------------------------------------------
# pnl_conversion_rate — USD per unit of quote currency
# ---------------------------------------------------------------------------
class TestPnlConversionRate:
    def test_usd_quote_is_one(self):
        assert pnl_conversion_rate("EURUSD", {}) == 1.0
        assert pnl_conversion_rate("XAUUSD", {}) == 1.0

    def test_direct_quote_usd_symbol(self):
        assert pnl_conversion_rate("USDJPY", {"JPYUSD": 0.0063}) == pytest.approx(0.0063)

    def test_invert_asset_price(self):
        # only USDJPY known → JPYUSD = 1 / USDJPY
        r = pnl_conversion_rate("USDJPY", {"USDJPY": 157.013})
        assert r == pytest.approx(1 / 157.013)

    def test_cross_via_base_usd(self):
        # GBPJPY with GBPUSD and GBPJPY known → JPYUSD = GBPUSD / GBPJPY
        rates = {"GBPUSD": 1.27, "GBPJPY": 200.0}
        assert pnl_conversion_rate("GBPJPY", rates) == pytest.approx(1.27 / 200.0)

    def test_invert_usd_quoted_counterpart(self):
        # CHFUSD is daily-only in the feed, but USDCHF is a live spot tick →
        # CHFUSD = 1 / USDCHF. This is what un-blanks EURCHF/AUDCHF/CADCHF.
        assert pnl_conversion_rate("EURCHF", {"USDCHF": 0.8195}) == pytest.approx(1 / 0.8195)
        assert pnl_conversion_rate("AUDCHF", {"USDCHF": 0.8195}) == pytest.approx(1 / 0.8195)
        assert pnl_conversion_rate("CADCHF", {"USDCHF": 0.8195}) == pytest.approx(1 / 0.8195)

    def test_direct_quote_usd_wins_over_inverse(self):
        # When the direct <quote>USD leg exists it is preferred (no inversion).
        rates = {"CHFUSD": 1.2174, "USDCHF": 0.8195}
        assert pnl_conversion_rate("EURCHF", rates) == pytest.approx(1.2174)

    def test_unavailable_returns_none(self):
        assert pnl_conversion_rate("USDJPY", {}) is None
        assert pnl_conversion_rate("GBPJPY", {"EURUSD": 1.1}) is None


# ---------------------------------------------------------------------------
# pnl_breakdown — the exact acceptance case + separation of components
# ---------------------------------------------------------------------------
class TestPnlBreakdown:
    def test_usdjpy_sell_acceptance_case(self):
        """USDJPY SELL 0.02, 157.024 → 157.013 gross ≈ $0.1401 (NOT $22)."""
        bd = pnl_breakdown("USDJPY", "SELL", 157.024, 157.013, 0.02,
                           settings=_settings(),
                           rates={"USDJPY": 157.013})
        assert bd.conversion_unavailable is False
        assert bd.gross == pytest.approx(0.1401, abs=1e-3)
        assert bd.quote_currency == "JPY"
        # sanity: the raw yen figure is the number the old code stored as USD
        raw_yen = 0.011 * 0.02 * 100_000
        assert raw_yen == pytest.approx(22.0)
        assert bd.gross < raw_yen / 100.0

    def test_usdjpy_buy(self):
        # BUY 0.02 from 157.013 → 157.024 is the mirror profit
        bd = pnl_breakdown("USDJPY", "BUY", 157.013, 157.024, 0.02,
                           settings=_settings(), rates={"USDJPY": 157.013})
        assert bd.gross == pytest.approx(0.1401, abs=1e-3)

    def test_usdjpy_sell_losing_is_negative_usd(self):
        bd = pnl_breakdown("USDJPY", "SELL", 157.013, 157.024, 0.02,
                           settings=_settings(), rates={"USDJPY": 157.013})
        assert bd.gross == pytest.approx(-0.1401, abs=1e-3)

    def test_eurusd_buy_no_rate_needed(self):
        # 0.10 lots, +0.0100 → $100 regardless of rates
        bd = pnl_breakdown("EURUSD", "BUY", 1.0850, 1.0950, 0.10,
                           settings=_settings(), rates={})
        assert bd.gross == pytest.approx(100.0)
        assert bd.quote_currency == "USD"

    def test_eurusd_sell_no_rate_needed(self):
        bd = pnl_breakdown("EURUSD", "SELL", 1.0950, 1.0850, 0.10,
                           settings=_settings(), rates={})
        assert bd.gross == pytest.approx(100.0)

    def test_gbpjpy_cross_uses_trusted_rate(self):
        # 0.10 lots, +1.00 yen → ¥10,000; at JPYUSD=0.0063 → $63
        bd = pnl_breakdown("GBPJPY", "BUY", 200.0, 201.0, 0.10,
                           settings=_settings(),
                           rates={"GBPUSD": 1.27, "GBPJPY": 200.0})
        assert bd.quote_currency == "JPY"
        assert bd.gross == pytest.approx(10_000.0 * (1.27 / 200.0), rel=1e-6)
        assert bd.gross == pytest.approx(63.5, abs=0.5)

    def test_xauusd_is_usd(self):
        # 0.05 lots, +10 → 100 oz/lot × 0.05 × 10 = $50
        bd = pnl_breakdown("XAUUSD", "BUY", 2400.0, 2410.0, 0.05,
                           settings=_settings(), rates={})
        assert bd.gross == pytest.approx(50.0)
        assert bd.quote_currency == "USD"

    def test_conversion_unavailable_fails_closed(self):
        bd = pnl_breakdown("USDJPY", "SELL", 157.024, 157.013, 0.02,
                           settings=_settings(), rates={})
        assert bd.conversion_unavailable is True
        assert bd.gross is None and bd.net is None and bd.spread is None
        assert bd.quote_currency == "JPY"

    def test_components_are_separated_and_net(self):
        # spread_cost = exit_mult × spread × lots × contract ; commission = 2×per_lot×lots
        s = _settings(paper_exit_spread_mult=1.0, paper_commission_per_lot=7.0,
                      spread_overrides={"USDJPY": 0.015})
        bd = pnl_breakdown("USDJPY", "SELL", 157.024, 157.013, 0.02,
                           settings=s, rates={"USDJPY": 157.013})
        # spread_raw (yen) = 1.0 × 0.015 × 0.02 × 100000 = ¥30 → /157.013 ≈ 0.1911
        assert bd.spread == pytest.approx(30.0 / 157.013, rel=1e-3)
        # commission is USD/lot → 2 × 7.0 × 0.02 = 0.28
        assert bd.commission == pytest.approx(0.28, rel=1e-6)
        assert bd.net == pytest.approx(bd.gross - bd.spread - bd.commission, abs=1e-6)


# ---------------------------------------------------------------------------
# PaperBrokerPnl facade — must match and must fail closed
# ---------------------------------------------------------------------------
class TestPaperBrokerPnlFacade:
    def test_compute_matches_user_example(self):
        pos = _pos("USDJPY", "SELL", 157.024, 157.013, 0.02)
        got = PaperBrokerPnl.compute(pos, _settings(), asset="USDJPY",
                                     rates={"USDJPY": 157.013})
        assert got == pytest.approx(0.1401, abs=1e-3)

    def test_compute_none_without_rate(self):
        pos = _pos("USDJPY", "SELL", 157.024, 157.013, 0.02)
        assert PaperBrokerPnl.compute(pos, _settings(), asset="USDJPY",
                                      rates={}) is None

    def test_compute_usd_quote_without_rate(self):
        pos = _pos("EURUSD", "BUY", 1.0850, 1.0950, 0.10)
        assert PaperBrokerPnl.compute(pos, _settings(), asset="EURUSD",
                                      rates={}) == pytest.approx(100.0)

    def test_gross_compute_is_raw_quote_currency(self):
        # gross_compute stays in the QUOTE currency (yen here) — it is the
        # pre-conversion number, kept for diagnostics only.
        pos = _pos("USDJPY", "SELL", 157.024, 157.013, 0.02)
        assert PaperBrokerPnl.gross_compute(pos, asset="USDJPY") == pytest.approx(22.0)


# ---------------------------------------------------------------------------
# fetch_pnl_rates — the shared one-shot rate-map builder
# ---------------------------------------------------------------------------
class TestFetchPnlRates:
    @pytest.fixture(autouse=True)
    def _reset_conv_cache(self):
        from app.services import execution as ex
        ex._conv_rates_cache.clear()
        yield
        ex._conv_rates_cache.clear()

    async def test_seed_is_reused_and_only_legs_fetched(self, monkeypatch):
        from app.services import execution as ex
        from app.integrations import quotes as q

        calls: list[list[str]] = []

        async def fake_spot(assets):
            calls.append(list(assets))
            return ({"JPYUSD": 1 / 157.013}, {})

        monkeypatch.setattr(q, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(q, "spot_source", lambda a: "yahoo")

        out = await ex.fetch_pnl_rates(["USDJPY"], seed={"USDJPY": 157.013})
        # seeded asset kept, its JPYUSD leg fetched (not the asset again)
        assert out["USDJPY"] == pytest.approx(157.013)
        assert out["JPYUSD"] == pytest.approx(1 / 157.013)
        assert calls and calls[0] == ["JPYUSD"]

    async def test_usd_quote_needs_no_fetch(self, monkeypatch):
        from app.services import execution as ex
        from app.integrations import quotes as q

        async def boom(assets):  # must never be called
            raise AssertionError("no fetch expected for USD-quote assets")

        monkeypatch.setattr(q, "fetch_spot_prices", boom)
        out = await ex.fetch_pnl_rates(["EURUSD"], seed={"EURUSD": 1.085})
        assert out == {"EURUSD": 1.085}

    async def test_daily_fallback_rate_is_refused(self, monkeypatch):
        from app.services import execution as ex
        from app.integrations import quotes as q

        async def fake_spot(assets):
            return ({"JPYUSD": 0.0063}, {})

        monkeypatch.setattr(q, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(q, "spot_source", lambda a: "daily")
        out = await ex.fetch_pnl_rates(["USDJPY"], seed={"USDJPY": 157.013})
        assert "JPYUSD" not in out  # daily rate must never price a journal PnL

    async def test_feed_failure_is_soft(self, monkeypatch):
        from app.services import execution as ex
        from app.integrations import quotes as q

        async def dead(assets):
            raise RuntimeError("offline")

        monkeypatch.setattr(q, "fetch_spot_prices", dead)
        out = await ex.fetch_pnl_rates(["USDJPY"], seed={"USDJPY": 157.013})
        assert out == {"USDJPY": 157.013}  # seed survives, no crash

    async def test_chf_cross_requests_inverse_and_base_legs(self, monkeypatch):
        """EURCHF must ask for USDCHF (inverse) and EURUSD (base) so the
        conversion can be derived even though CHFUSD is daily-only."""
        from app.services import execution as ex
        from app.integrations import quotes as q

        calls: list[list[str]] = []

        async def fake_spot(assets):
            calls.append(list(assets))
            return ({"USDCHF": 0.8195, "EURUSD": 1.1464}, {})

        monkeypatch.setattr(q, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(q, "spot_source", lambda a: "spot")

        out = await ex.fetch_pnl_rates(["EURCHF"], seed={"EURCHF": 0.9387})
        assert calls and set(calls[0]) == {"CHFUSD", "USDCHF", "EURUSD"}
        # the inverse identity resolves the rate end-to-end
        assert ex.pnl_conversion_rate("EURCHF", out) == pytest.approx(1 / 0.8195)

    async def test_chf_cross_resolves_when_only_usdchf_is_spot(self, monkeypatch):
        """CHFUSD daily-only + USDCHF spot → EURCHF still gets a rate."""
        from app.services import execution as ex
        from app.integrations import quotes as q

        async def fake_spot(assets):
            return ({"CHFUSD": 1.2174, "USDCHF": 0.8195, "EURUSD": 1.1464}, {})

        def src(a):
            return "daily" if a == "CHFUSD" else "spot"

        monkeypatch.setattr(q, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(q, "spot_source", src)

        out = await ex.fetch_pnl_rates(["EURCHF"], seed={"EURCHF": 0.9387})
        assert "CHFUSD" not in out  # daily rate refused
        assert out["USDCHF"] == pytest.approx(0.8195)
        assert ex.pnl_conversion_rate("EURCHF", out) == pytest.approx(1 / 0.8195)

    async def test_conversion_legs_reuse_ten_minute_cache(self, monkeypatch):
        """Prod 2026-09-24: per-minute leg fetches burned the exchangerate
        quota. Legs reuse for 10 min; trade assets still fetch fresh."""
        from app.services import execution as ex
        from app.integrations import quotes as q

        ex._conv_rates_cache.clear()
        calls: list[list[str]] = []

        async def fake_spot(assets):
            calls.append(list(assets))
            return ({"JPYUSD": 1 / 157.013}, {})

        monkeypatch.setattr(q, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(q, "spot_source", lambda a: "spot")

        out1 = await ex.fetch_pnl_rates(["USDJPY"], seed={"USDJPY": 157.013})
        assert out1["JPYUSD"] == pytest.approx(1 / 157.013)
        assert len(calls) == 1
        out2 = await ex.fetch_pnl_rates(["USDJPY"], seed={"USDJPY": 157.013})
        assert out2["JPYUSD"] == pytest.approx(1 / 157.013)
        assert len(calls) == 1  # leg served from cache, no second fetch
        ex._conv_rates_cache.clear()
