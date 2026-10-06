"""Triple-barrier labeling — the measurement layer this project never had.

WHY (owner 2026-10-06)
----------------------
Every SL/TP the platform has ever used was a GUESS: `sl_distance_min_pct`
0.5 → 0.65 was chosen by reading 44 losing trades, `rr_target` 1.5 → 2.0 by
arithmetic on a 36% win rate. The evidence that neither was right:

  * 44 closed trades, avg −0.239R, WR 36.4%
  * the BEST winner ever banked +1.06R — **no trade has ever reached the
    1.5R target**, so the TP is a number nobody has ever seen hit
  * 21 of 45 trades opened with a stop BELOW the then-active 0.5% floor,
    i.e. the "clamp" was not in force when they were opened
  * `signals` only spans 11 days (2026-09-25 → 10-06) and joins to just 25
    usable outcomes; the 95% CI on that win rate is [20.8%, 59.2%] — the
    data cannot distinguish 36% from 50%

So the stop width and the target are the two numbers most in need of
evidence, and triple-barrier labeling is how you get it (López de Prado,
*Advances in Financial Machine Learning*, ch. 3): instead of asserting
"the stop is 0.65% and the target is 2R", place barriers at a GRID of widths,
let the data say which barrier the market touched FIRST, and read the win
rate and expectancy off the resulting label distribution.

WHAT THIS MODULE DOES (and does not)
-------------------------------------
Pure math over a candle sequence. No DB, no network, no settings. Given an
entry, a direction and a barrier spec, it walks the bars that come AFTER the
entry and reports which barrier was touched first, after how many bars, and
what that was worth in R.

It deliberately does NOT model the live exit stack (partial close, trailing
ladder, breakeven, left-behind time stop). Those are *policies* applied on
top of a position; this measures the *signal*. Comparing the two is the
point: if the signal's own expectancy is negative, no exit policy rescues it,
and if the signal is fine, the exit stack is what needs fixing.

INTRABAR AMBIGUITY — the honest caveat
--------------------------------------
Daily bars carry one high and one low with no ordering. When a single bar's
range contains BOTH barriers, the path is unknowable. This module resolves
it the conservative way (assume the stop printed first) and FLAGS the event
via ``BarrierOutcome.ambiguous`` so a study can report the rate. On daily FX
bars with a 1.5R target this rate is material — a study that hides it is
lying by omission, so ``sweep_barriers`` reports it as a column.

GAP POLICY
----------
When a bar OPENS beyond a barrier (weekend/overnight gap), the fill is the
OPEN, not the barrier price. Filling at the barrier would understate the
loss, which is how backtests end up looking better than reality.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "BarrierSpec",
    "BarrierOutcome",
    "BarrierSweepRow",
    "LabelledEvent",
    "SL_HIT",
    "TP_HIT",
    "EXPIRED",
    "PENDING",
    "label_event",
    "label_events",
    "barrier_grid",
    "summarise",
    "summarise_cell",
    "sweep_barriers",
    "sweep_grid",
    "grid_key",
    "label_grid",
    "opportunity_table",
]

SL_HIT = "sl"
TP_HIT = "tp"
EXPIRED = "expired"
PENDING = "pending"


@dataclass(frozen=True)
class BarrierSpec:
    """One cell of the barrier grid.

    ``sl_width`` / ``tp_width`` are ABSOLUTE price distances from the entry
    (not percentages) so a spec computed from ATR at signal time stays valid
    as price drifts. ``tp_width`` is usually ``sl_width * tp_r``; keeping them
    independent lets the grid cross a 1.0R target against a 2.0R stop, which
    is exactly the asymmetry that produced the "winners never run" result.

    ``max_bars`` is the VERTICAL barrier (López de Prado): the trade is
    abandoned if neither price barrier is reached within this many bars. It
    matters because a stop-only study systematically over-rewards trades that
    would have been cut by the live time stop (``no_behind_min_days`` /
    ``max_hold_days``).
    """

    sl_width: float
    tp_width: float
    max_bars: int = 20


@dataclass(frozen=True)
class BarrierOutcome:
    """Result of walking the bars after one entry."""

    label: str
    r_multiple: float
    bars_held: int
    exit_price: float
    ambiguous: bool = False
    gap_fill: bool = False
    mfe_r: float = 0.0      # best unrealised R reached (uses bar extremes)
    mae_r: float = 0.0      # worst unrealised R reached (uses bar extremes)

    @property
    def is_resolved(self) -> bool:
        """True when a price barrier decided the trade (not the clock)."""
        return self.label in (SL_HIT, TP_HIT)


@dataclass
class BarrierSweepRow:
    """One (sl, tp) cell of the grid, with its aggregate statistics."""

    sl_width: float
    tp_width: float
    tp_r: float
    max_bars: int
    n: int
    n_tp: int
    n_sl: int
    n_expired: int
    n_ambiguous: int
    sum_r: float
    mean_r: float
    win_rate: float
    mean_win_r: float
    mean_loss_r: float
    payoff: float
    #: expectancy using ONLY trades a price barrier resolved — the number that
    #: answers "is this signal profitable if the exit policy gets out of the
    #: way", free of the vertical barrier's influence.
    mean_r_resolved: float

    def as_row(self) -> dict[str, Any]:
        return {
            "sl_pct": round(self.sl_width, 6),
            "tp_r": round(self.tp_r, 2),
            "n": self.n,
            "n_tp": self.n_tp,
            "n_sl": self.n_sl,
            "n_expired": self.n_expired,
            "n_ambiguous": self.n_ambiguous,
            "ambiguous_pct": round(100.0 * self.n_ambiguous / self.n, 1) if self.n else 0.0,
            "win_rate_pct": round(100.0 * self.win_rate, 1),
            "mean_r": round(self.mean_r, 3),
            "mean_r_resolved": round(self.mean_r_resolved, 3),
            "payoff": round(self.payoff, 2),
            "sum_r": round(self.sum_r, 2),
        }


def _direction_sign(direction: Any) -> int:
    """+1 for BUY, −1 for SELL. Unrecognised input → BUY (fail-safe, logged
    by the caller; a study on garbage input must not silently flip sides)."""
    return 1 if str(direction or "BUY").strip().upper() != "SELL" else -1


def _finite(x: float) -> bool:
    return math.isfinite(x)


def _bar_values(candle: Any) -> tuple[float, float, float, float]:
    """Accept a Candle dataclass, a dict, or a 4-tuple. Never raises."""
    if isinstance(candle, dict):
        return (float(candle.get("o", candle.get("open", 0)) or 0),
                float(candle.get("h", candle.get("high", 0)) or 0),
                float(candle.get("l", candle.get("low", 0)) or 0),
                float(candle.get("c", candle.get("close", 0)) or 0))
    if isinstance(candle, (list, tuple)) and len(candle) >= 4:
        return float(candle[0]), float(candle[1]), float(candle[2]), float(candle[3])
    return (float(getattr(candle, "o", 0) or 0), float(getattr(candle, "h", 0) or 0),
            float(getattr(candle, "l", 0) or 0), float(getattr(candle, "c", 0) or 0))


def label_event(direction: Any, entry: float, spec: BarrierSpec,
                future_candles: Sequence[Any]) -> BarrierOutcome:
    """Label ONE event. ``future_candles`` = the bars strictly AFTER the entry.

    Returns ``label=PENDING`` when there is not enough future data to decide
    (the study reports those separately rather than counting them as losses —
    the difference matters: an unlabelled tail would otherwise drag every
    expectancy toward zero for reasons that have nothing to do with the edge).

    Never raises. Bad widths, a non-positive entry or a malformed bar yields
    PENDING instead of an exception, so one bad row cannot abort a 50,000-row
    replay.
    """
    try:
        entry = float(entry)
        sl_w = float(getattr(spec, "sl_width", 0) or 0)
        tp_w = float(getattr(spec, "tp_width", 0) or 0)
        max_bars = int(getattr(spec, "max_bars", 0) or 0)
    except (TypeError, ValueError):
        return BarrierOutcome(PENDING, 0.0, 0, 0.0)
    # NaN fails every comparison, so `entry <= 0` does NOT catch it — an
    # unguarded NaN would sail through the validation and then produce NaN
    # R values that quietly poison a study's mean. Test finiteness explicitly.
    if not all(_finite(x) for x in (entry, sl_w, tp_w)):
        return BarrierOutcome(PENDING, 0.0, 0, 0.0)
    if entry <= 0 or sl_w <= 0 or max_bars <= 0 or tp_w < 0:
        return BarrierOutcome(PENDING, 0.0, 0, entry)

    sign = _direction_sign(direction)
    stop_price = entry - sign * sl_w
    tp_price = entry + sign * tp_w

    # Track excursion with the bar extremes so the study can report how much
    # unrealised profit each signal actually offered — the quantity the live
    # exit stack is accused of leaving on the table.
    mfe_r = 0.0
    mae_r = 0.0
    bars = list(future_candles or [])[:max_bars]

    for i, raw in enumerate(bars, start=1):
        o, h, l, c = _bar_values(raw)
        if o <= 0 and h <= 0 and l <= 0 and c <= 0:
            continue
        hi, lo = (h, l) if h >= l else (l, h)
        if hi > 0:
            mfe_r = max(mfe_r, sign * (hi - entry) / sl_w)
        if lo > 0:
            mae_r = min(mae_r, sign * (lo - entry) / sl_w)

        # --- gap: the bar opened beyond a barrier, fill at the OPEN ---
        # Sign matters: a gap THROUGH the stop is a LOSS, so the R is signed
        # against the position, not measured from the barrier price. Getting
        # this backwards books a −3R stop-out as a +3R win.
        if sign > 0:
            if o > 0 and o <= stop_price:
                return BarrierOutcome(SL_HIT, (o - entry) / sl_w, i, o,
                                      gap_fill=True, mfe_r=mfe_r, mae_r=mae_r)
            if o > 0 and o >= tp_price:
                return BarrierOutcome(TP_HIT, tp_w / sl_w, i, o,
                                      gap_fill=True, mfe_r=mfe_r, mae_r=mae_r)
            hit_sl, hit_tp = lo <= stop_price, hi >= tp_price
        else:
            if o > 0 and o >= stop_price:
                return BarrierOutcome(SL_HIT, (entry - o) / sl_w, i, o,
                                      gap_fill=True, mfe_r=mfe_r, mae_r=mae_r)
            if o > 0 and o <= tp_price:
                return BarrierOutcome(TP_HIT, tp_w / sl_w, i, o,
                                      gap_fill=True, mfe_r=mfe_r, mae_r=mae_r)
            hit_sl, hit_tp = hi >= stop_price, lo <= tp_price

        if hit_sl and hit_tp:
            # Undecidable from daily bars — resolve pessimistically.
            return BarrierOutcome(SL_HIT, -1.0, i, stop_price, ambiguous=True,
                                  mfe_r=mfe_r, mae_r=mae_r)
        if hit_sl:
            r = (stop_price - entry) / sl_w * sign
            return BarrierOutcome(SL_HIT, r, i, stop_price,
                                  mfe_r=mfe_r, mae_r=mae_r)
        if hit_tp:
            return BarrierOutcome(TP_HIT, tp_w / sl_w, i, tp_price,
                                  mfe_r=mfe_r, mae_r=mae_r)

    # --- vertical barrier: nothing resolved within max_bars ---
    if bars:
        last = _bar_values(bars[-1])
        exit_px = last[3] or last[2] or entry
        return BarrierOutcome(EXPIRED, sign * (exit_px - entry) / sl_w, len(bars),
                              exit_px, mfe_r=mfe_r, mae_r=mae_r)
    return BarrierOutcome(PENDING, 0.0, 0, entry)


@dataclass
class LabelledEvent:
    """One replayed signal plus every label the grid produced for it."""

    asset: str
    direction: str
    bar_index: int
    entry: float
    atr_pct: float = 0.0
    opportunity: float = 0.0
    confidence: float = 0.0
    #: the bars strictly AFTER this entry — the only thing label_event reads
    future: Sequence = field(default_factory=list)
    features: dict = field(default_factory=dict)
    outcomes: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.asset}|{self.direction}|{self.bar_index}|{self.entry:.6f}"


def label_events(events: Iterable[Any], spec_of: Any) -> list[LabelledEvent]:
    """Apply ``spec_of(event) -> BarrierSpec`` to each event and keep the label.

    ``spec_of`` is injected so the same events can be swept across the grid
    without re-running the (expensive) replay.
    """
    out: list[LabelledEvent] = []
    for ev in events or []:
        try:
            spec = spec_of(ev)
        except Exception:
            continue
        if spec is None:
            continue
        res = label_event(ev.direction, ev.entry, spec, ev.future)
        ev.outcomes[f"{spec.sl_width:.6f}|{spec.tp_width:.6f}|{spec.max_bars}"] = res
        out.append(ev)
    return out


def barrier_grid(sl_multiples: Sequence[float], tp_rs: Sequence[float],
                 atr_pct: float, entry: float,
                 max_bars: int = 20) -> list[BarrierSpec]:
    """Cross an ATR-multiple stop grid with an R-multiple target grid.

    The stop is sized from ATR (the signal's OWN volatility measure) rather
    than a flat percentage of price, because that is what makes the result
    comparable across EURUSD and GBPNZD. ``tp_rs`` may include values below
    or above 1.0 — including ``tp_r < 1`` is the point: if the best cell sits
    under 1R then the live 1.5R/2.0R targets were asking for a move the pair
    does not make in the holding window.
    """
    specs: list[BarrierSpec] = []
    atr = float(entry or 0) * float(atr_pct or 0) / 100.0
    for m in sl_multiples or []:
        sl_w = float(m) * atr
        if sl_w <= 0:
            continue
        for r in tp_rs or []:
            specs.append(BarrierSpec(sl_w, sl_w * float(r), int(max_bars)))
    return specs


def summarise(events: Sequence[LabelledEvent], spec: BarrierSpec) -> BarrierSweepRow:
    """Aggregate one grid cell. Never raises.

    Two statistics come out of every cell and they answer different questions:

    ``mean_r``
        every labelled trade, vertical barrier included — "what would this
        have PAID with a plain stop / target / clock".
    ``mean_r_resolved``
        only the trades a PRICE barrier decided — "is the signal itself
        profitable once the exit policy is taken out of the picture".

    A cell can rank well on one and badly on the other when the clock does
    most of the work, so both are reported and neither can be cherry-picked.
    """
    key = f"{spec.sl_width:.6f}|{spec.tp_width:.6f}|{spec.max_bars}"
    n = n_tp = n_sl = n_exp = n_amb = 0
    sum_r = 0.0
    rs: list[float] = []
    resolved_rs: list[float] = []
    for ev in events or []:
        res = ev.outcomes.get(key)
        if res is None or res.label == PENDING:
            continue
        n += 1
        sum_r += res.r_multiple
        rs.append(res.r_multiple)
        if res.ambiguous:
            n_amb += 1
        if res.label == TP_HIT:
            n_tp += 1
            resolved_rs.append(res.r_multiple)
        elif res.label == SL_HIT:
            n_sl += 1
            resolved_rs.append(res.r_multiple)
        elif res.label == EXPIRED:
            n_exp += 1
    if not n:
        return BarrierSweepRow(
            sl_width=spec.sl_width, tp_width=spec.tp_width,
            tp_r=(spec.tp_width / spec.sl_width if spec.sl_width else 0.0),
            max_bars=spec.max_bars, n=0, n_tp=0, n_sl=0, n_expired=0,
            n_ambiguous=0, sum_r=0.0, mean_r=0.0, win_rate=0.0,
            mean_win_r=0.0, mean_loss_r=0.0, payoff=0.0, mean_r_resolved=0.0)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    mw = sum(wins) / len(wins) if wins else 0.0
    ml = sum(losses) / len(losses) if losses else 0.0
    return BarrierSweepRow(
        sl_width=spec.sl_width, tp_width=spec.tp_width,
        tp_r=(spec.tp_width / spec.sl_width if spec.sl_width else 0.0),
        max_bars=spec.max_bars, n=n, n_tp=n_tp, n_sl=n_sl, n_expired=n_exp,
        n_ambiguous=n_amb, sum_r=sum_r, mean_r=sum_r / n,
        win_rate=len(wins) / n, mean_win_r=mw, mean_loss_r=ml,
        payoff=(mw / abs(ml) if ml else 0.0),
        mean_r_resolved=(sum(resolved_rs) / len(resolved_rs) if resolved_rs else 0.0),
    )


def sweep_barriers(events: Sequence[LabelledEvent],
                   specs: Sequence[BarrierSpec]) -> list[BarrierSweepRow]:
    """Every spec against every event. Returns rows sorted best-mean-R first."""
    rows = [summarise(events, sp) for sp in specs or []]
    rows.sort(key=lambda r: (-r.mean_r, -r.n))
    return rows


def opportunity_table(events: Sequence[LabelledEvent],
                      specs: Sequence[BarrierSpec]) -> list[BarrierSweepRow]:
    """Like :func:`sweep_barriers` but ranked on ``mean_r_resolved``.

    Use this to answer "what is this WORTH" and
    :func:`sweep_barriers` to answer "what would it have PAID" — a cell can
    look good on one and terrible on the other when the vertical barrier does
    a lot of the work, and reporting only the flattering one is how a study
    turns into a rationalisation.
    """
    rows = [summarise(events, sp) for sp in specs or []]
    rows.sort(key=lambda r: (-r.mean_r_resolved, -r.n))
    return rows


# ---------------------------------------------------------------------------
# Grid-coordinate sweep
# ---------------------------------------------------------------------------
# WHY A SEPARATE KEY: the absolute-width key above is correct only when every
# event shares one ATR. Across a portfolio it never does — EURUSD ATR ~0.5%,
# GBPNZD ~1.1% — so grouping by absolute width silently puts EVERY event in
# its own row: n=1 per cell, and a sweep over such rows ranks noise. Cells
# must be identified by their GRID COORDINATES (ATR multiple × R target ×
# clock) so all events line up on the same comparison.


def grid_key(sl_mult: float, tp_r: float, max_bars: int) -> str:
    """Canonical key for one grid cell. Coordinates, never price widths."""
    return f"{float(sl_mult):.4f}|{float(tp_r):.4f}|{int(max_bars)}"


def label_grid(event: LabelledEvent, sl_multiples: Sequence[float],
               tp_rs: Sequence[float], max_bars: Sequence[int]) -> int:
    """Label ONE event across the whole coordinate grid. Returns cells filled."""
    atr = float(event.entry or 0) * float(event.atr_pct or 0) / 100.0
    if atr <= 0:
        return 0
    n = 0
    for m in sl_multiples or []:
        sl_w = float(m) * atr
        if sl_w <= 0:
            continue
        for r in tp_rs or []:
            for mb in max_bars or []:
                sp = BarrierSpec(sl_w, sl_w * float(r), int(mb))
                event.outcomes[grid_key(m, r, mb)] = label_event(
                    event.direction, event.entry, sp, event.future)
                n += 1
    return n


def summarise_cell(events: Sequence[LabelledEvent], sl_mult: float, tp_r: float,
                   max_bars: int) -> BarrierSweepRow:
    """Aggregate one coordinate cell across every event."""
    key = grid_key(sl_mult, tp_r, max_bars)
    n = n_tp = n_sl = n_exp = n_amb = 0
    sum_r = 0.0
    rs: list[float] = []
    resolved_rs: list[float] = []
    for ev in events or []:
        res = ev.outcomes.get(key)
        if res is None or res.label == PENDING:
            continue
        n += 1
        sum_r += res.r_multiple
        rs.append(res.r_multiple)
        if res.ambiguous:
            n_amb += 1
        if res.label == TP_HIT:
            n_tp += 1
            resolved_rs.append(res.r_multiple)
        elif res.label == SL_HIT:
            n_sl += 1
            resolved_rs.append(res.r_multiple)
        elif res.label == EXPIRED:
            n_exp += 1
    sl_mult = float(sl_mult)
    if not n:
        return BarrierSweepRow(sl_mult, sl_mult * float(tp_r), float(tp_r),
                               int(max_bars), 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0,
                               0.0, 0.0, 0.0)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    mw = sum(wins) / len(wins) if wins else 0.0
    ml = sum(losses) / len(losses) if losses else 0.0
    return BarrierSweepRow(
        sl_width=sl_mult, tp_width=sl_mult * float(tp_r), tp_r=float(tp_r),
        max_bars=int(max_bars), n=n, n_tp=n_tp, n_sl=n_sl, n_expired=n_exp,
        n_ambiguous=n_amb, sum_r=sum_r, mean_r=sum_r / n,
        win_rate=len(wins) / n, mean_win_r=mw, mean_loss_r=ml,
        payoff=(mw / abs(ml) if ml else 0.0),
        mean_r_resolved=(sum(resolved_rs) / len(resolved_rs) if resolved_rs else 0.0),
    )


def sweep_grid(events: Sequence[LabelledEvent], sl_multiples: Sequence[float],
               tp_rs: Sequence[float], max_bars: Sequence[int],
               min_n: int = 0, rank_by_resolved: bool = False
               ) -> list[BarrierSweepRow]:
    """Sweep the coordinate grid. Ranked best-first on mean R (or on
    ``mean_r_resolved`` when ``rank_by_resolved``). Cells below ``min_n`` are
    dropped — a cell measured on a handful of trades ranks above real ones by
    luck alone."""
    rows = []
    for m in sl_multiples or []:
        for r in tp_rs or []:
            for mb in max_bars or []:
                row = summarise_cell(events, m, r, mb)
                if row.n >= max(1, int(min_n or 0)):
                    rows.append(row)
    key = (lambda r: (-r.mean_r_resolved, -r.n)) if rank_by_resolved \
        else (lambda r: (-r.mean_r, -r.n))
    rows.sort(key=key)
    return rows
