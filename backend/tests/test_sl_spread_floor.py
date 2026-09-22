"""P0-6 — a stop that lives INSIDE the bid-ask spread is not a stop (2026-09-22).

Prod evidence (PAPER-000083, USDJPY SELL):
    entry 157.024  SL 157.029  TP 157.0165  volume 0.02  capital $500 @ 2%
    → SL distance 0.005 price units (~0.5 pip) vs a realistic USDJPY spread of
      0.015 (~1.5 pip) ⇒ BOTH the SL and the TP sat inside the spread, so the
      next tick filled the TP 43 seconds after entry and the journal booked a
      fake +$22 (+4.4% of capital in 43s; 1R was only $10).

Root cause: ``sl_cap_distance`` = budget ÷ (min_lot × contract) = 10 ÷ 2000 =
0.005 units — a risk-budget cap that knows nothing about the spread, applied
by ``apply_sl_cap`` (tighten-only) straight onto an already-small structural
stop. Nothing in ``effective_sl_tp`` / ``execute_signal`` ever compared the
final stop distance against the symbol's spread.

The fix adds three pieces:
  1. ``spread_sl_floor`` = SPREAD_SL_FLOOR_MULT × effective_spread — a sanity
     floor every effective SL must clear (step 3 of ``effective_sl_tp``; the
     ONE sanctioned widening, because a sub-spread stop is a bug not a plan).
  2. ``sl_cap_distance`` is itself floored by ``spread_sl_floor`` so the cap
     can never squeeze a stop inside the spread.
  3. ``sl_cap_below_spread`` + the P0-6 block in ``execute_signal`` — when the
     risk BUDGET cannot fund a k×spread stop at the min lot there is no valid
     size, so the order is BLOCKED with ``sl_narrower_than_spread`` rather
     than opened into an instant fill.
"""
import pytest

from app.models.schemas import (
    DEFAULT_SPREADS,
    SPREAD_SL_FLOOR_MULT,
    AppSettings,
    apply_sl_cap,
    effective_min_lot,
    effective_sl_tp,
    effective_spread,
    sl_cap_below_spread,
    sl_cap_distance,
    spread_sl_floor,
)
from app.workers import portfolio_monitor


def cfg(**over) -> AppSettings:
    """Settings mirroring the prod row that produced PAPER-000083."""
    base = dict(
        capital=500.0, risk_per_trade_pct=2.0, min_lot=0.02,
        sl_distance_mode="short", sl_cap_enabled=True, rr_target=1.5,
        kill_daily_loss_pct=500.0,
    )
    base.update(over)
    return AppSettings(**base)


# ---------------------------------------------------------------------------
# 1. spread_sl_floor — the k × spread sanity floor
# ---------------------------------------------------------------------------
class TestSpreadSlFloor:
    def test_floor_is_k_times_the_builtin_spread(self):
        s = cfg()
        for asset, spread in (("EURUSD", DEFAULT_SPREADS["EURUSD"]),
                              ("USDJPY", DEFAULT_SPREADS["USDJPY"]),
                              ("GBPJPY", DEFAULT_SPREADS["GBPJPY"]),
                              ("XAUUSD", DEFAULT_SPREADS["XAUUSD"])):
            assert spread_sl_floor(s, asset) == pytest.approx(
                SPREAD_SL_FLOOR_MULT * spread, rel=1e-9), asset

    def test_mult_is_at_least_one_spread(self):
        # A floor of exactly 1× spread still leaves only 0.5× of adverse room
        # after the entry half-spread — the whole point of P0-6 is that this
        # is NOT enough. Guard against a future "optimisation" lowering it.
        assert SPREAD_SL_FLOOR_MULT >= 2.0

    def test_zeroed_symbol_has_no_floor(self):
        """A symbol the user deliberately zeroed stays permissive (0.0)."""
        s = cfg(spread_overrides={"USDJPY": 0.0})
        assert effective_spread(s, "USDJPY") == 0.0
        assert spread_sl_floor(s, "USDJPY") == 0.0

    def test_floor_prefers_the_user_override(self):
        s = cfg(spread_overrides={"USDJPY": 0.05})
        assert spread_sl_floor(s, "USDJPY") == pytest.approx(
            SPREAD_SL_FLOOR_MULT * 0.05, rel=1e-9)

    def test_broken_settings_return_zero(self):
        # Pydantic rejects a non-numeric override at construction time, so an
        # unusable value can only arrive via a broken/partial settings object.
        class Broken:
            spread_overrides = {"USDJPY": "nonsense"}
            paper_spread = 0.0
        assert spread_sl_floor(Broken(), "USDJPY") == pytest.approx(
            SPREAD_SL_FLOOR_MULT * DEFAULT_SPREADS["USDJPY"], rel=1e-9)
        # None settings → the resolver reads paper_spread via getattr defaults
        # and still lands on the built-in table entry (0.015 × 3 = 0.045).
        assert spread_sl_floor(None, "USDJPY") == pytest.approx(0.045, rel=1e-9)


# ---------------------------------------------------------------------------
# 2. sl_cap_distance — the cap can no longer sit inside the spread
# ---------------------------------------------------------------------------
class TestSlCapDistanceSpreadFloor:
    def test_raw_cap_is_unchanged_when_it_already_clears_the_spread(self):
        """EURUSD: raw cap 0.005 ≫ 3×0.0001 → the cap is untouched."""
        s = cfg()
        assert sl_cap_distance(s, "EURUSD") == pytest.approx(
            10.0 / (0.02 * 100_000.0), rel=1e-9)
        assert sl_cap_distance(s, "EURUSD") > spread_sl_floor(s, "EURUSD")

    def test_cap_is_raised_to_the_spread_floor_when_the_budget_is_tiny(self):
        """A budget that only buys a sub-spread stop gets the floor instead.

        Capital $0.50 @ 2% = $0.01 budget, min lot 0.02, contract 100k → raw
        cap 0.000005. On XAUUSD (spread 0.30, floor 0.90) the cap MUST be
        raised to 0.90 — a 0.000005 gold stop is millions× inside the spread.
        """
        s = cfg(capital=0.50)
        raw = 0.01 / (0.02 * 100_000.0)
        cap = sl_cap_distance(s, "XAUUSD")
        assert raw == pytest.approx(0.000005, rel=1e-9)
        assert cap == pytest.approx(SPREAD_SL_FLOOR_MULT * 0.30, rel=1e-9)
        assert cap > raw

    def test_zeroed_symbol_keeps_the_raw_cap(self):
        s = cfg(spread_overrides={"USDJPY": 0.0})
        assert sl_cap_distance(s, "USDJPY") == pytest.approx(0.005, rel=1e-9)

    def test_usdjpy_cap_is_converted_from_yen_to_usd(self):
        """USDJPY's quote is JPY, so dist × lots × 100k is in YEN.

        Feeding the raw product as if it were USD made the cap 0.005 units
        (0.0032% of price) instead of 0.785 (0.5%) — a ~157× over-tightening
        that turned every normal tick into 1–3R and fired the TP1 partial and
        the trailing stop within seconds. With ``price`` the cap solves the
        budget in USD and lands at 0.5% of price, risking the full $10.
        """
        s = cfg(spread_overrides={})
        price = 156.996
        cap = sl_cap_distance(s, "USDJPY", price=price)
        # budget 10 / (0.02 × 100k × (1/156.996)) = 0.78498
        assert cap == pytest.approx(10.0 / (0.02 * 100_000.0 / price),
                                    rel=1e-6)
        # ≈ 0.5% of price — a tradeable stop, not 0.0032%
        assert cap == pytest.approx(price * 0.005, rel=1e-3)
        assert cap > 0.7

    def test_disabled_cap_is_still_zero(self):
        s = cfg(sl_cap_enabled=False)
        assert sl_cap_distance(s, "XAUUSD") == 0.0

    def test_cap_never_narrows_a_stop_below_the_floor(self):
        """apply_sl_cap stays tighten-only — but its ceiling now clears the
        floor, so a "capped" stop can never land inside the spread."""
        s = cfg()
        sl, dist, capped = apply_sl_cap(
            s, 2400.0, 2300.0, "XAUUSD", "BUY")
        if capped:
            assert dist >= spread_sl_floor(s, "XAUUSD") - 1e-12


# ---------------------------------------------------------------------------
# 3. sl_cap_below_spread — the fail-closed detector
# ---------------------------------------------------------------------------
class TestSlCapBelowSpread:
    def test_true_when_the_budget_cannot_fund_a_k_spread_stop(self):
        """With no price the yen product is read as USD → cap 0.005.

        Legacy/no-price callers keep the old (conservative) behaviour: the
        naive cap 0.005 sits below the floor → True. Real callers pass the
        entry price and get the USD-correct cap instead (next test).
        """
        s = cfg()
        assert sl_cap_below_spread(s, "USDJPY") is True

    def test_false_for_usdjpy_when_the_price_is_supplied(self):
        """The real execution path: cap 0.785 ≫ floor 0.045 → no block.

        execute_signal calls sl_cap_below_spread(s, asset, price=entry); the
        FX-correct cap clears the spread floor, so a normal USDJPY trade is
        NOT blocked. The naive no-price call would have wrongly blocked it.
        """
        s = cfg(spread_overrides={})
        assert sl_cap_below_spread(s, "USDJPY", price=156.996) is False

    def test_false_when_the_budget_clears_the_floor(self):
        """EURUSD floor 0.0003 < cap 0.005 → False (normal majors unaffected)."""
        s = cfg()
        assert sl_cap_below_spread(s, "EURUSD") is False
        assert sl_cap_below_spread(s, "GBPNZD") is False
        assert sl_cap_below_spread(s, "AUDNZD") is False

    def test_false_when_the_cap_feature_is_off(self):
        """With the cap off the spread floor still applies in effective_sl_tp,
        but there is no budget squeeze to detect → no extra block here."""
        s = cfg(sl_cap_enabled=False)
        assert sl_cap_below_spread(s, "XAUUSD") is False

    def test_false_for_a_big_account(self):
        """$50k × 2% = $1000 budget → USDJPY cap 0.5 ≫ floor 0.045 → False."""
        s = cfg(capital=50_000.0)
        assert sl_cap_below_spread(s, "USDJPY", price=156.996) is False

    def test_false_when_the_symbol_has_no_spread(self):
        s = cfg(spread_overrides={"USDJPY": 0.0})
        assert sl_cap_below_spread(s, "USDJPY") is False


# ---------------------------------------------------------------------------
# 4. effective_sl_tp step 3 — the sanctioned widening
# ---------------------------------------------------------------------------
class TestEffectiveSlTpSpreadFloor:
    def test_paper_000083_repro_widens_the_stop_to_k_spread(self):
        """The exact prod row, but with USDJPY's REAL spread (override cleared).

        Pre-fix: SL 0.005 units (~0.5 pip) → TP filled in 43s.
        Post-fix: SL raised to 3 × 0.015 = 0.045 units and TP follows at the
        row's RR, so the stop now sits 1.5 pips BEYOND the spread edge.
        """
        s = cfg(spread_overrides={})  # realistic USDJPY spread (0.015)
        sl, tp, dist, tiered, capped = effective_sl_tp(
            s, 157.024, 157.029, 157.0165, "USDJPY", "SELL")
        assert dist == pytest.approx(SPREAD_SL_FLOOR_MULT * 0.015, rel=1e-9)
        assert sl == pytest.approx(round(157.024 + dist, 5), abs=1e-9)
        # row RR = 0.0075 / 0.005 = 1.5 preserved on the widened distance
        assert tp == pytest.approx(round(157.024 - dist * 1.5, 5), abs=1e-4)
        assert dist > effective_spread(s, "USDJPY")

    def test_wide_stop_is_left_alone(self):
        """A normal 40-pip FX stop is far wider than the floor → unchanged
        apart from the short-mode tier (medium 40 pips → short 26.7 pips, still
        ≫ the 0.0003 EURUSD floor, so the floor does NOT fire)."""
        s = cfg(spread_overrides={})
        sl, tp, dist, tiered, _ = effective_sl_tp(
            s, 1.0850, 1.0800, 1.0950, "EURUSD", "BUY")
        assert tiered
        assert dist == pytest.approx(0.0050 * (1.0 / 1.5), rel=1e-9)
        assert dist > spread_sl_floor(s, "EURUSD")
        assert sl == pytest.approx(round(1.0850 - dist, 5), abs=1e-9)

    def test_cap_and_floor_reconcile_when_both_apply(self):
        """cap 0.92 > floor 0.90 (XAUUSD) → the CAP wins (tighten-only holds),
        and the result still clears the spread floor."""
        s = cfg(capital=2000.0, risk_per_trade_pct=2.0, min_lot=0.01,
                min_lot_gold=0.01, sl_distance_mode="long")
        sl, tp, dist, tiered, capped = effective_sl_tp(
            s, 2400.0, 2320.0, 2520.0, "XAUUSD", "BUY")
        assert capped
        assert dist == pytest.approx(sl_cap_distance(s, "XAUUSD"), rel=1e-9)
        assert dist >= spread_sl_floor(s, "XAUUSD") - 1e-12
        # cap 4.0? gold contract 100 → 40/(0.01*100)=40 → far above the floor
        assert dist > spread_sl_floor(s, "XAUUSD")

    def test_floor_does_not_fire_when_cap_is_off_for_a_plan(self):
        """apply_cap=False is extended-open's reviewed plan leg — but the
        spread floor is NOT part of the cap and still applies, because a
        sub-spread plan stop is a bug on any path."""
        s = cfg(spread_overrides={})
        sl, tp, dist, _, capped = effective_sl_tp(
            s, 157.024, 157.029, 157.0165, "USDJPY", "SELL",
            apply_cap=False)
        assert not capped
        assert dist == pytest.approx(SPREAD_SL_FLOOR_MULT * 0.015, rel=1e-9)

    def test_buy_direction_widening_points_the_right_way(self):
        s = cfg(spread_overrides={})
        sl, tp, dist, _, _ = effective_sl_tp(
            s, 157.024, 157.019, 157.0315, "USDJPY", "BUY")
        assert dist == pytest.approx(SPREAD_SL_FLOOR_MULT * 0.015, rel=1e-9)
        assert sl == pytest.approx(round(157.024 - dist, 5), abs=1e-9)
        assert sl < 157.024 < tp

    def test_failsafe_inputs_still_pass_through(self):
        s = cfg()
        out = effective_sl_tp(s, 0.0, 0.0, 0.0, "USDJPY", "BUY")
        assert out == (0.0, 0.0, 0.0, False, False)

    def test_floor_uses_the_same_spread_the_fill_uses(self):
        """Lockstep: the fill (apply_spread at effective_spread) and the SL
        floor must read the SAME number, or the guard measures a spread the
        broker never charges."""
        s = cfg()
        sp = effective_spread(s, "USDJPY")
        assert spread_sl_floor(s, "USDJPY") == pytest.approx(
            SPREAD_SL_FLOOR_MULT * sp, rel=1e-9)
        # min-lot risk at the floored stop ≈ 3× the spread cost of the fill
        contract = 100_000.0
        lot = effective_min_lot(s, "USDJPY")
        risk = spread_sl_floor(s, "USDJPY") * lot * contract
        assert risk == pytest.approx(0.045 * 0.02 * 100_000.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 5. FX currency conversion — dist × lots × contract is in the QUOTE currency
# ---------------------------------------------------------------------------
class TestRiskCurrencyConversion:
    def test_usd_per_quote_unit(self):
        from app.models.schemas import usd_per_quote_unit
        # USD-quoted → the product already IS dollars
        assert usd_per_quote_unit("EURUSD", 1.08) == pytest.approx(1.0)
        assert usd_per_quote_unit("XAUUSD", 2400.0) == pytest.approx(1.0)
        # USD-base (USDJPY): one quote unit (¥1) is 1/price dollars
        assert usd_per_quote_unit("USDJPY", 156.996) == pytest.approx(
            1.0 / 156.996, rel=1e-9)
        # cross with no derivable rate → fail-closed (None)
        assert usd_per_quote_unit("GBPJPY", 189.5) is None
        assert usd_per_quote_unit("AUDNZD", 1.09) is None
        # no price → None (can't convert)
        assert usd_per_quote_unit("USDJPY", None) is None

    def test_risk_usd_of_distance_usdjpy(self):
        from app.models.schemas import risk_usd_of_distance
        # 0.785 × 0.02 × 100k = ¥1570 nominal → ÷156.996 ≈ $10
        risk = risk_usd_of_distance(0.785, 0.02, "USDJPY", 156.996)
        assert risk == pytest.approx(0.785 * 0.02 * 100_000.0 / 156.996,
                                     rel=1e-6)
        assert risk == pytest.approx(10.0, rel=1e-2)

    def test_risk_usd_of_distance_usd_quoted_is_unchanged(self):
        from app.models.schemas import risk_usd_of_distance
        assert risk_usd_of_distance(0.002, 0.05, "EURUSD", 1.08) == \
            pytest.approx(0.002 * 0.05 * 100_000.0, rel=1e-9)
        # cross → None (caller falls back)
        assert risk_usd_of_distance(1.0, 0.02, "GBPJPY", 189.5) is None

    def test_risk_to_lot_for_usdjpy_uses_the_yen_rate(self):
        from app.models.schemas import risk_to_lot_for
        # capital 500 @ 2% = $10 budget; dist 0.785, price 156.996
        # 10 / (0.785 × 100k × (1/156.996)) ≈ 0.02 lots
        lots = risk_to_lot_for(500.0, 2.0, 0.785, "USDJPY", price=156.996)
        assert lots == pytest.approx(0.02, abs=0.005)
        # without the price the yen product is treated as USD → ~0.0
        assert risk_to_lot_for(500.0, 2.0, 0.785, "USDJPY") == 0.0

    def test_risk_to_lot_for_regression_gold_and_eurusd(self):
        from app.models.schemas import risk_to_lot_for
        # gold: 100 oz/lot; $10 / (5.0 × 100) = 0.02
        assert risk_to_lot_for(500.0, 2.0, 5.0, "XAUUSD", price=2400.0) == \
            pytest.approx(0.02, abs=0.005)
        # EURUSD unchanged by the conversion (quote == USD)
        assert risk_to_lot_for(500.0, 2.0, 0.002, "EURUSD", price=1.08) == \
            pytest.approx(0.05, abs=0.005)
        assert risk_to_lot_for(500.0, 2.0, 0.002, "EURUSD") == \
            pytest.approx(0.05, abs=0.005)

    # -- open-risk / pause-gate sites (portfolio_monitor, chat, monitor card) --
    def test_open_risk_sum_for_a_usdjpy_leg_is_converted_to_usd(self):
        """The monitor's open-risk sum feeds RiskEngine.check(); a USDJPY
        leg's nominal ¥ product must NOT be read as dollars, or open_risk_pct
        blows past the daily limit and raises a FAKE trading pause."""
        from app.models.schemas import contract_value_for, risk_usd_of_distance
        # 0.785 × 0.02 × 100k = ¥1570 nominal; naive USD read ≈ $1570,
        # converted ≈ $10 — a 157× overstatement of a single 2% leg.
        dist, lots, entry = 0.785, 0.02, 156.996
        naive = dist * lots * contract_value_for("USDJPY")
        fixed = risk_usd_of_distance(dist, lots, "USDJPY", entry)
        assert naive == pytest.approx(1_570.0, rel=1e-6)
        assert fixed == pytest.approx(10.0, rel=1e-2)
        # On a $500 account the naive figure (314%) dwarfs a 2% budget; the
        # converted figure (~2%) is on the same order as a single trade.
        cap = 500.0
        assert naive / cap * 100.0 > 100.0
        assert fixed / cap * 100.0 == pytest.approx(2.0, abs=0.05)

    def test_open_risk_sum_falls_back_naively_for_a_cross(self):
        """Crosses can't derive a rate → the caller keeps the naive product
        (fail-safe), so the change never silently zeroes real risk."""
        from app.models.schemas import contract_value_for, risk_usd_of_distance
        assert risk_usd_of_distance(1.0, 0.02, "GBPJPY", 189.5) is None
        # caller fallback (same shape as the three fixed loops)
        fallback = 1.0 * 0.02 * contract_value_for("GBPJPY")
        assert fallback == pytest.approx(2_000.0, rel=1e-6)

    def test_usd_quoted_open_risk_is_unchanged(self):
        from app.models.schemas import contract_value_for, risk_usd_of_distance
        # EURUSD/XAUUSD legs already in USD — identical before/after.
        for asset, dist, lots, entry in (
                ("EURUSD", 0.002, 0.05, 1.08),
                ("XAUUSD", 5.0, 0.02, 2400.0)):
            assert risk_usd_of_distance(dist, lots, asset, entry) == \
                pytest.approx(dist * lots * contract_value_for(asset), rel=1e-9)


# ---------------------------------------------------------------------------
# 6. End-to-end: the monitor's pause verdict must not be fabricated by a
#    non-USD-quote leg (2026-09-22 — same class as the SL-cap / heat fix).
# ---------------------------------------------------------------------------
def _monitor_db(rows: list[dict], capital: float = 500.0,
                risk_pct: float = 2.0, daily_limit: float = 2.0):
    from app.api.routes.settings import persist_settings
    from tests.test_auto_trader import db_with_client

    db = db_with_client({
        "equity_snapshots": [
            {"id": "eq-1", "snapshot_date": "2026-09-01", "equity": capital},
        ],
    })
    assert persist_settings(db, AppSettings(
        capital=capital, risk_per_trade_pct=risk_pct,
        kill_daily_loss_pct=daily_limit))
    db.rows["paper_trades"] = rows
    return db


def _flat_broker(equity: float = 500.0):
    from types import SimpleNamespace

    class _Broker:
        def all_positions(self):
            return []

        def account_summary(self):
            return SimpleNamespace(equity=equity)

    return _Broker()


def test_monitor_open_risk_does_not_fake_a_pause_for_a_usdjpy_leg():
    """A small USDJPY leg (~$0.37 real risk = 0.07% of $500) must NOT read as
    $58 (11.6%, over a 5% ceiling) and fabricate a trading pause."""
    from tests.test_limit_expand import RecordingNotifier

    # 5% ceiling so the real 0.07% leg + the 2% new-trade headroom (2.07%)
    # stays well under; only the naive $58 read (11.6%) could breach it.
    db = _monitor_db([{
        "id": "j1", "status": "open", "asset": "USDJPY", "direction": "buy",
        "volume": 0.02, "entry_price": 156.996, "stop_loss": 156.996 - 0.029,
        "created_at": "2026-09-22T00:00:00+00:00",
    }], daily_limit=5.0)

    out = portfolio_monitor.monitor_once(db, _flat_broker(), RecordingNotifier())
    assert out["breach"] is False
    assert db._client.store.get("trading_pause") is None


def test_monitor_open_risk_still_counts_a_genuinely_oversized_leg():
    """Sanity: the FX-aware sum must still trip when risk is REALLY over the
    budget (a $500 account with a $30 EURUSD leg at a 2% limit)."""
    from tests.test_limit_expand import RecordingNotifier

    db = _monitor_db([{
        "id": "j2", "status": "open", "asset": "EURUSD", "direction": "buy",
        "volume": 0.15, "entry_price": 1.0800, "stop_loss": 1.0780,
        "created_at": "2026-09-22T00:00:00+00:00",
    }])

    out = portfolio_monitor.monitor_once(db, _flat_broker(), RecordingNotifier())
    # 0.0020 × 0.15 × 100k = $30 = 6% of $500 > the 2% daily budget.
    assert out["breach"] is True

