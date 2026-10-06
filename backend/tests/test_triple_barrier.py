"""Triple-barrier labeling — pure math, no DB, no network.

Run from backend/:
    C:/Python314/python.exe -m pytest tests/test_triple_barrier.py -v

These tests exist because every number the barrier study will report flows
through :func:`label_event`. A silent error there would produce a
plausible-looking expectancy and a wrong stop width, which is precisely the
failure mode this module was written to eliminate.
"""
from __future__ import annotations

import pytest

from app.engine.triple_barrier import (
    EXPIRED,
    PENDING,
    SL_HIT,
    TP_HIT,
    LabelledEvent,
    barrier_grid,
    label_event,
    summarise,
    sweep_barriers,
)
from app.integrations.quotes import Candle


def bar(o, h, l, c):
    return Candle(o=o, h=h, l=l, c=c)


def spec(sl_pct=1.0, tp_r=2.0, max_bars=20, entry=100.0):
    sl = entry * sl_pct / 100.0
    return sl, sl * tp_r, max_bars


# ---------------------------------------------------------------------------
# Basic resolution
# ---------------------------------------------------------------------------
class TestLabelEvent:
    def test_buy_hits_tp_first(self):
        sl, tp, mb = spec()
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            bar(100, 100.2, 99.9, 100.1),          # inside both
            bar(100.1, 102.5, 100.0, 102.0),      # clears +2.0 TP
        ])
        assert out.label == TP_HIT
        assert out.bars_held == 2
        assert out.r_multiple == pytest.approx(2.0)
        assert out.ambiguous is False
        assert out.mfe_r > 2.0

    def test_buy_hits_sl_first(self):
        sl, tp, mb = spec()
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            bar(100, 100.1, 98.9, 99.0),           # clears -1.0 stop
        ])
        assert out.label == SL_HIT
        assert out.r_multiple == pytest.approx(-1.0)
        assert out.bars_held == 1

    def test_sell_is_mirrored(self):
        sl, tp, mb = spec()
        # SELL: stop ABOVE entry (+1.0), target BELOW (-2.0)
        out = label_event("SELL", 100.0, _spec(sl, tp, mb), [
            bar(100, 100.1, 97.5, 97.8),           # dives through the target
        ])
        assert out.label == TP_HIT
        assert out.r_multiple == pytest.approx(2.0)

    def test_sell_stop_is_above_entry(self):
        sl, tp, mb = spec()
        out = label_event("SELL", 100.0, _spec(sl, tp, mb), [
            bar(100, 101.5, 99.9, 101.0),          # rips through the stop
        ])
        assert out.label == SL_HIT
        assert out.r_multiple == pytest.approx(-1.0)


def _spec(sl, tp, mb):
    from app.engine.triple_barrier import BarrierSpec
    return BarrierSpec(sl, tp, mb)


# ---------------------------------------------------------------------------
# Intrabar ambiguity — the honesty guarantee
# ---------------------------------------------------------------------------
class TestAmbiguity:
    def test_both_barriers_in_one_bar_resolves_to_stop(self):
        """One daily bar spanning both barriers is undecidable. The module
        must resolve pessimistically AND flag it, so a study can publish the
        rate instead of quietly benefiting from favourable guesses."""
        sl, tp, mb = spec()
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            bar(100, 103.0, 98.0, 101.0),          # range covers 99 and 102
        ])
        assert out.label == SL_HIT
        assert out.ambiguous is True
        assert out.r_multiple == pytest.approx(-1.0)

    def test_ambiguity_is_not_flagged_when_only_one_barrier_touches(self):
        sl, tp, mb = spec()
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            bar(100, 101.0, 98.0, 99.5),           # stop only
        ])
        assert out.label == SL_HIT
        assert out.ambiguous is False

    def test_gap_fill_uses_the_open_not_the_barrier(self):
        """A weekend gap through the stop fills at the OPEN. Filling at the
        barrier price would understate the loss — the classic way a backtest
        ends up prettier than reality."""
        sl, tp, mb = spec()
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            bar(97.0, 97.5, 96.0, 96.5),           # opens far below the 99 stop
        ])
        assert out.label == SL_HIT
        assert out.gap_fill is True
        assert out.exit_price == pytest.approx(97.0)
        # -3.0 / 1.0 = -3R, NOT the -1R a barrier-price fill would report
        assert out.r_multiple == pytest.approx(-3.0)

    def test_gap_through_target_also_fills_at_open(self):
        sl, tp, mb = spec()
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            bar(105.0, 106.0, 104.5, 105.5),       # opens above the 102 target
        ])
        assert out.label == TP_HIT
        assert out.gap_fill is True
        assert out.r_multiple == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Vertical barrier
# ---------------------------------------------------------------------------
class TestVerticalBarrier:
    def test_expires_at_close_of_last_bar(self):
        sl, tp, mb = spec(max_bars=3)
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            bar(100, 100.2, 99.9, 100.1),
            bar(100.1, 100.4, 100.0, 100.3),
            bar(100.3, 100.5, 100.2, 100.4),       # +0.4 at the deadline
        ])
        assert out.label == EXPIRED
        assert out.bars_held == 3
        assert out.r_multiple == pytest.approx(0.4)

    def test_bars_beyond_the_vertical_barrier_are_ignored(self):
        """A barrier touched AFTER max_bars must not resolve the trade."""
        sl, tp, mb = spec(max_bars=2)
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            bar(100, 100.1, 99.9, 100.0),
            bar(100.0, 100.1, 99.9, 100.0),
            bar(100.0, 105.0, 99.9, 104.0),       # would have been a TP
        ])
        assert out.label == EXPIRED
        assert out.bars_held == 2

    def test_no_future_bars_is_pending_not_a_loss(self):
        """An unlabelled tail must not be silently scored as a loss — that
        would drag every expectancy toward zero for reasons unrelated to
        the edge."""
        sl, tp, mb = spec()
        assert label_event("BUY", 100.0, _spec(sl, tp, mb), []).label == PENDING

    def test_pending_is_excluded_from_summaries(self):
        sl, tp, mb = spec()
        sp = _spec(sl, tp, mb)
        ev = LabelledEvent(asset="EURUSD", direction="BUY", bar_index=0, entry=100.0,
                           atr_pct=1.0, future=[])
        row = summarise([ev], sp)
        assert row.n == 0
        assert row.mean_r == 0.0
        assert row.win_rate == 0.0


# ---------------------------------------------------------------------------
# Excursion tracking (what the exit stack left on the table)
# ---------------------------------------------------------------------------
class TestExcursions:
    def test_mfe_records_the_best_the_trade_ever_offered(self):
        """A signal that ran to +1.5R before coming back is the exact case
        the live trailing/left-behind rules are accused of truncating."""
        sl, tp, mb = spec(tp_r=2.0)
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            bar(100, 101.6, 99.9, 101.5),          # +1.6R, no barrier hit
            bar(101.5, 101.6, 100.5, 100.6),
            bar(100.6, 100.7, 98.5, 99.0),          # comes back through the stop
        ])
        assert out.label == SL_HIT
        assert out.mfe_r == pytest.approx(1.6)
        assert out.mae_r == pytest.approx(-1.5)

    def test_mae_records_the_worst_drawdown_on_a_winner(self):
        """A trade can be 3.0 below entry and still reach its target when the
        stop is 5.0 wide. MAE is reported in R, so that excursion is −0.6R,
        not −3R — getting the units wrong here would make every study report
        drawdowns N times too large."""
        from app.engine.triple_barrier import BarrierSpec
        sp = BarrierSpec(sl_width=5.0, tp_width=10.0, max_bars=3)   # 2R
        out = label_event("BUY", 100.0, sp, [
            bar(100, 100.1, 97.0, 97.2),           # -3.0 price, stop at 95.0
            bar(97.2, 102.0, 97.1, 101.0),
            bar(101.0, 110.5, 100.9, 110.0),       # clears the +10.0 target
        ])
        assert out.label == TP_HIT
        assert out.mae_r == pytest.approx(-0.6)   # (97.0 - 100.0) / 5.0

    def test_mfe_mae_are_in_r_units_not_price(self):
        from app.engine.triple_barrier import BarrierSpec
        sp = BarrierSpec(sl_width=2.0, tp_width=4.0, max_bars=2)
        out = label_event("BUY", 100.0, sp, [
            bar(100, 106.0, 96.0, 105.0),          # +3R high, -2R low
            bar(105.0, 108.0, 104.0, 107.0),
        ])
        assert out.mfe_r == pytest.approx(3.0)
        assert out.mae_r == pytest.approx(-2.0)


# ---------------------------------------------------------------------------
# Robustness — a 50k-row replay must not die on one bad row
# ---------------------------------------------------------------------------
class TestNeverRaises:
    @pytest.mark.parametrize("entry", [0.0, -1.0, None, "abc", float("nan")])
    def test_bad_entry_is_pending(self, entry):
        sl, tp, mb = spec()
        assert label_event("BUY", entry, _spec(sl, tp, mb),
                           [bar(1, 2, 0.5, 1.5)]).label == PENDING

    @pytest.mark.parametrize("sl,tp,mb", [(0.0, 2.0, 10), (-1.0, 2.0, 10),
                                          (1.0, -1.0, 10), (1.0, 2.0, 0)])
    def test_bad_widths_are_pending(self, sl, tp, mb):
        assert label_event("BUY", 100.0, _spec(sl, tp, mb),
                           [bar(100, 101, 99, 100)]).label == PENDING

    def test_malformed_bar_is_skipped(self):
        sl, tp, mb = spec()
        out = label_event("BUY", 100.0, _spec(sl, tp, mb), [
            object(), bar(100, 102.5, 100.0, 102.0),
        ])
        assert out.label == TP_HIT

    def test_dict_and_tuple_bars_are_accepted(self):
        """The live labeler will pass dicts from Postgres; the replay passes
        Candle objects. Both must work."""
        sl, tp, mb = spec()
        for bar_like in (
            {"o": 100, "h": 102.5, "l": 100.0, "c": 102.0},
            (100, 102.5, 100.0, 102.0),
        ):
            out = label_event("BUY", 100.0, _spec(sl, tp, mb), [bar_like])
            assert out.label == TP_HIT

    def test_unknown_direction_defaults_to_buy(self):
        sl, tp, mb = spec()
        out = label_event(None, 100.0, _spec(sl, tp, mb), [bar(100, 102.5, 100, 102)])
        assert out.label == TP_HIT


# ---------------------------------------------------------------------------
# Grid + sweep
# ---------------------------------------------------------------------------
class TestGridAndSweep:
    def test_grid_crosses_sl_multiples_with_r_targets(self):
        specs = barrier_grid([1.0, 1.5], [1.0, 2.0], atr_pct=1.0, entry=100.0)
        assert len(specs) == 4
        assert specs[0].sl_width == pytest.approx(1.0)      # 1.0 x 1% of 100
        assert specs[0].tp_width == pytest.approx(1.0)      # 1R
        assert specs[1].tp_width == pytest.approx(2.0)      # 2R
        assert specs[2].sl_width == pytest.approx(1.5)

    def test_zero_atr_yields_no_specs(self):
        assert barrier_grid([1.0], [2.0], atr_pct=0.0, entry=100.0) == []

    def test_sweep_ranks_best_mean_r_first(self):
        """The sweep's whole job is ordering grid cells by what they would
        have paid. Construct two cells with known, different means and assert
        both the ordering AND the arithmetic — a sort that works on made-up
        numbers can still be sorting the wrong column."""
        from app.engine.triple_barrier import BarrierSpec

        def mk(asset, direction, entry, bars):
            return LabelledEvent(asset=asset, direction=direction, bar_index=0,
                                 entry=entry, atr_pct=1.0, future=bars)

        # "Good" cell — 6 trades reach +3R, 4 are stopped at -1R:
        #   mean = (6*3 + 4*-1) / 10 = +1.4
        good_bars = [bar(100, 100.9, 99.8, 100.8),      # stop 99.0, target 100.9
                     bar(100.8, 103.5, 100.7, 103.0)]
        bad_bars = [bar(100, 100.1, 98.5, 98.8)]         # through the 99.0 stop
        good = BarrierSpec(sl_width=0.3, tp_width=0.9, max_bars=5)
        # "Bad" cell — same 10 trades but a stop so wide the winners only
        # reach +1R and expire: mean = (6*1 + 4*-1) / 10 = +0.2
        wide_bars_win = [bar(100, 101.8, 99.9, 101.5), bar(101.5, 102.0, 100.5, 101.0)]
        wide_bars_lose = [bar(100, 100.1, 98.8, 99.0)]
        wide = BarrierSpec(sl_width=1.0, tp_width=3.0, max_bars=2)

        events = []
        for _ in range(6):
            events.append(mk("EURUSD", "BUY", 100.0, good_bars))
        for _ in range(4):
            events.append(mk("EURUSD", "BUY", 100.0, bad_bars))
        for i, ev in enumerate(events):
            for sp, future in ((good, good_bars if i < 6 else bad_bars),
                               (wide, wide_bars_win if i < 6 else wide_bars_lose)):
                ev.outcomes[f"{sp.sl_width:.6f}|{sp.tp_width:.6f}|{sp.max_bars}"] = \
                    label_event(ev.direction, ev.entry, sp, future)

        rows = sweep_barriers(events, [wide, good])
        assert [r.sl_width for r in rows] == [pytest.approx(0.3), pytest.approx(1.0)]
        assert rows[0].mean_r == pytest.approx(1.4)
        assert rows[1].mean_r == pytest.approx(0.2)
        assert rows[0].win_rate == pytest.approx(0.6)
        assert rows[0].payoff == pytest.approx(3.0)

    def test_summarise_reports_ambiguity_rate(self):
        from app.engine.triple_barrier import BarrierSpec
        sp = BarrierSpec(sl_width=1.0, tp_width=2.0, max_bars=5)
        evs = []
        for bars in ([bar(100, 103.0, 98.0, 101.0)],      # ambiguous
                     [bar(100, 103.0, 99.5, 102.5)]):     # clean TP
            evs.append(LabelledEvent(asset="EURUSD", direction="BUY", bar_index=0,
                                     entry=100.0, atr_pct=1.0, future=bars,
                                     outcomes={
                                         "1.000000|2.000000|5":
                                             label_event("BUY", 100.0, sp, bars)}))
        row = summarise(evs, sp)
        assert row.n == 2
        assert row.n_ambiguous == 1
        assert row.as_row()["ambiguous_pct"] == 50.0

    def test_resolved_stat_ignores_the_vertical_barrier(self):
        """mean_r_resolved must exclude clock-outs, mean_r must not — a cell
        that looks fine only because the clock did the work has to be
        visible as such."""
        from app.engine.triple_barrier import BarrierSpec
        sp = BarrierSpec(sl_width=1.0, tp_width=2.0, max_bars=2)
        evs = []
        # 1 clean TP (+2R), 1 clean SL (-1R), 2 clock-outs at +0.5R each
        plans = [[bar(100, 103.0, 100.0, 102.5)],
                 [bar(100, 100.0, 98.0, 98.5)],
                 [bar(100, 100.6, 99.9, 100.5), bar(100.5, 100.6, 100.4, 100.5)],
                 [bar(100, 100.6, 99.9, 100.5), bar(100.5, 100.6, 100.4, 100.5)]]
        for bars in plans:
            evs.append(LabelledEvent(asset="EURUSD", direction="BUY", bar_index=0,
                                     entry=100.0, atr_pct=1.0, future=bars,
                                     outcomes={
                                         "1.000000|2.000000|2":
                                             label_event("BUY", 100.0, sp, bars)}))
        row = summarise(evs, sp)
        assert row.n == 4
        assert row.n_expired == 2
        # mean_r includes the clock-outs: (2 - 1 + 0.5 + 0.5) / 4 = 0.5
        assert row.mean_r == pytest.approx(0.5)
        # mean_r_resolved excludes them: (2 - 1) / 2 = 0.5
        assert row.mean_r_resolved == pytest.approx(0.5)
