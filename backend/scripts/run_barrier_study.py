"""Barrier study — what stop width and target does the signal ACTUALLY want?

Run from backend/:
    C:/Python314/python.exe scripts/run_barrier_study.py
    C:/Python314/python.exe scripts/run_barrier_study.py --days 1095 --min-n 200

WHY
---
`sl_distance_min_pct` 0.5 → 0.65 and `rr_target` 1.5 → 2.0 were both chosen by
reading 44 losing trades. This asks the data instead: replay the live signal
generator over years of daily candles, place barriers on a grid, and let the
label distribution say which cell was worth money.

READ THE CAVEATS (printed above the tables, not buried). The two that matter
most: daily bars cannot see intrabar order, and replayed confidence carries NO
news penalty, so every win rate here is optimistic versus production.

This script only READS. It never writes settings, never opens an order and
never touches the live database — its output is a proposal for a human.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.engine.strategy_replay import DEFAULT_WARMUP, REPLAY_CAVEATS, replay
from app.engine.triple_barrier import (
    grid_key,
    label_event,
    label_grid,
    summarise_cell,
    sweep_grid,
)

DEFAULT_ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD",
                  "EURCHF", "AUDCHF", "CADCHF", "GBPCHF"]

#: Live `sl_distance_mode` is "medium" → SL = 1.5 × ATR before the 0.65% floor
#: binds, so 1.5 is the cell production effectively runs.
LIVE_SL_ATR_MULT = 1.5
LIVE_RR = 2.0

SL_MULTIPLES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
TP_RS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
MAX_BARS = (5, 10, 20)


def rule(title: str = "", width: int = 96) -> None:
    print(f"\n{title}\n{'-' * width}" if title else "-" * width)


async def fetch_all(assets, days):
    from app.integrations import quotes
    series = {}
    async with httpx.AsyncClient() as client:
        for asset in assets:
            try:
                candles = await quotes.fetch_candles(asset, client, days=days)
            except Exception as exc:
                print(f"  ! {asset}: {str(exc)[:66]}")
                continue
            if candles:
                series[asset] = candles
                print(f"  {asset:8} {len(candles):5} bars  "
                      f"{candles[0].c:.5f} -> {candles[-1].c:.5f}")
    return series


def print_top(rows, limit, headline):
    rule(f"TOP {limit} CELLS by {headline}")
    print(f"{'SLxATR':>7}{'TP R':>7}{'bars':>6}{'n':>6}{'WR%':>7}{'meanR':>8}"
          f"{'meanRes':>9}{'payoff':>8}{'ambig%':>8}{'sumR':>9}")
    for r in rows[:limit]:
        d = r.as_row()
        print(f"{r.sl_width:>7.2f}{r.tp_r:>7.2f}{r.max_bars:>6}{d['n']:>6}"
              f"{d['win_rate_pct']:>7.1f}{d['mean_r']:>8.3f}"
              f"{d['mean_r_resolved']:>9.3f}{d['payoff']:>8.2f}"
              f"{d['ambiguous_pct']:>8.1f}{d['sum_r']:>9.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--min-n", type=int, default=150)
    ap.add_argument("--min-opp", type=float, default=0.0)
    ap.add_argument("--min-conf", type=float, default=0.0)
    ap.add_argument("--cooldown", type=int, default=5)
    args = ap.parse_args()

    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    print(f"Barrier study — {len(assets)} assets, {args.days} days")
    rule()
    print("CAVEATS (these bound how good the numbers below can be):")
    for c in REPLAY_CAVEATS:
        print(f"  - {c}")
    rule()

    t0 = time.time()
    print("\nfetching history...")
    series = asyncio.run(fetch_all(assets, args.days))
    if not series:
        print("no history fetched — cannot study")
        return 1
    print(f"  ({time.time() - t0:.1f}s)")

    print(f"\nreplaying (warmup={DEFAULT_WARMUP}, cooldown={args.cooldown} bars)...")
    events = replay(series, warmup=DEFAULT_WARMUP, cooldown_bars=args.cooldown)
    print(f"  candidate events: {len(events)}")
    if not events:
        print("  no events — history too short for the warmup")
        return 1

    if args.min_opp or args.min_conf:
        before = len(events)
        events = [e for e in events
                  if e.opportunity >= args.min_opp and e.confidence >= args.min_conf]
        print(f"  gate opp>={args.min_opp} conf>={args.min_conf}: "
              f"{before} -> {len(events)}")

    cells = len(SL_MULTIPLES) * len(TP_RS) * len(MAX_BARS)
    print(f"\nlabelling {len(events)} events x {cells} cells...")
    t1 = time.time()
    for ev in events:
        label_grid(ev, SL_MULTIPLES, TP_RS, MAX_BARS)
    print(f"  ({time.time() - t1:.1f}s)")

    if len(events) < args.min_n:
        print(f"\n  !! only {len(events)} events (min_n={args.min_n}). "
              f"Ranking cells this small is how a study becomes a "
              f"rationalisation — widen --days or lower --cooldown, or read "
              f"the table as indicative only.")

    by_paid = sweep_grid(events, SL_MULTIPLES, TP_RS, MAX_BARS,
                         min_n=args.min_n)
    by_worth = sweep_grid(events, SL_MULTIPLES, TP_RS, MAX_BARS,
                          min_n=args.min_n, rank_by_resolved=True)

    print_top(by_paid, 12, "mean R — what it would have PAID")
    print_top(by_worth, 12, "mean R, price-resolved only — what the SIGNAL is worth")

    rule("WHERE PRODUCTION SITS (SL = %.1f x ATR floored 0.65%%, TP = %.1fR)"
         % (LIVE_SL_ATR_MULT, LIVE_RR))
    print(f"{'bars':>6}{'n':>6}{'WR%':>7}{'meanR':>8}{'sumR':>9}{'ambig%':>8}")
    for mb in MAX_BARS:
        r = summarise_cell(events, LIVE_SL_ATR_MULT, LIVE_RR, mb)
        d = r.as_row()
        print(f"{mb:>6}{d['n']:>6}{d['win_rate_pct']:>7.1f}{d['mean_r']:>8.3f}"
              f"{d['sum_r']:>9.2f}{d['ambiguous_pct']:>8.1f}")

    best = by_paid[0] if by_paid else None
    rule("THRESHOLD SWEEP at the best-paying cell")
    if not best:
        print("  no cell reached min_n — a threshold sweep here would be noise")
    else:
        print(f"  cell: SL={best.sl_width:.2f}xATR  TP={best.tp_r:.2f}R  "
              f"clock={best.max_bars} bars")
        print(f"{'minOpp':>8}{'minConf':>9}{'n':>6}{'WR%':>7}{'meanR':>8}{'sumR':>9}")
        for mo in (0, 40, 50, 60, 70):
            for mc in (0, 60, 70, 80):
                sub = [e for e in events
                       if e.opportunity >= mo and e.confidence >= mc]
                if len(sub) < 30:
                    continue
                r = summarise_cell(sub, best.sl_width, best.tp_r, best.max_bars)
                if not r.n:
                    continue
                d = r.as_row()
                print(f"{mo:>8}{mc:>9}{d['n']:>6}{d['win_rate_pct']:>7.1f}"
                      f"{d['mean_r']:>+8.3f}{d['sum_r']:>+9.2f}")

    rule("PER-ASSET at the best-paying cell")
    if best:
        print(f"{'asset':8}{'n':>6}{'WR%':>7}{'meanR':>8}{'sumR':>9}")
        for asset in sorted(series):
            sub = [e for e in events if e.asset == asset]
            r = summarise_cell(sub, best.sl_width, best.tp_r, best.max_bars)
            if not r.n:
                continue
            d = r.as_row()
            print(f"{asset:8}{d['n']:>6}{d['win_rate_pct']:>7.1f}"
                  f"{d['mean_r']:>+8.3f}{d['sum_r']:>+9.2f}")

    rule("FEATURE SEPARATION at the best-paying cell "
         "(does any feature rank the outcomes?)")
    if best:
        key = grid_key(best.sl_width, best.tp_r, best.max_bars)
        for feat in ("adx", "rsi", "atr_pct", "chg20", "ema_gap_atr",
                     "st_agree", "ema_agree", "macd_agree"):
            vals = [(e.features.get(feat), e.outcomes[key].r_multiple)
                    for e in events
                    if key in e.outcomes and e.features.get(feat) is not None]
            if len(vals) < 40:
                continue
            distinct = len({v for v, _ in vals})
            if distinct < 2:
                # A feature with ONE value carries no information — and
                # splitting it in half silently becomes a split by TIME,
                # which reads as a "finding" that is really just regime
                # change. Skipping is the honest move.
                print(f"  {feat:14} constant ({vals[0][0]}) — carries no "
                      f"information, skipped")
                continue
            vals.sort(key=lambda t: t[0])
            half = len(vals) // 2
            lo_r = [r for _, r in vals[:half]]
            hi_r = [r for _, r in vals[half:]]
            lo_m = sum(lo_r) / max(1, len(lo_r))
            hi_m = sum(hi_r) / max(1, len(hi_r))
            print(f"  {feat:14} ({distinct:3} distinct) low half meanR={lo_m:+.3f}"
                  f"  high half meanR={hi_m:+.3f}  spread={hi_m - lo_m:+.3f}")

    rule("MFE / MAE — is the target even reachable?")
    key20 = grid_key(SL_MULTIPLES[0], TP_RS[0], MAX_BARS[-1])
    vals = [e.outcomes[key20] for e in events if key20 in e.outcomes]
    if vals:
        def q(a, p):
            return a[min(len(a) - 1, int(len(a) * p))]
        mfes = sorted(v.mfe_r for v in vals)
        maes = sorted(v.mae_r for v in vals)
        print(f"  MFE (best unrealised R in {MAX_BARS[-1]} bars): "
              f"p25={q(mfes, .25):+.2f} med={q(mfes, .5):+.2f} "
              f"p75={q(mfes, .75):+.2f} p95={q(mfes, .95):+.2f} max={mfes[-1]:+.2f}")
        print(f"  MAE (worst drawdown R):                        "
              f"p25={q(maes, .25):+.2f} med={q(maes, .5):+.2f} "
              f"p75={q(maes, .75):+.2f} min={maes[0]:+.2f}")
        for r in (1.0, 1.5, 2.0, LIVE_RR):
            n = sum(1 for m in mfes if m >= r)
            print(f"  signals that EVER offered >= {r:.1f}R: {n}/{len(mfes)} "
                  f"({100.0 * n / len(mfes):.0f}%)")

    print(f"\ntotal {time.time() - t0:.1f}s")

    # ------------------------------------------------------------------
    # THE HONESTY GATE — chronological holdout.
    #
    # 192 cells were ranked on the SAME 576 events. The winner of a 192-way
    # race is biased upward by selection alone: with no real edge the top cell
    # still looks positive. The only way to tell an edge from a lucky cell is
    # to RANK on one period and SCORE on a later one, in time order. If the
    # winner collapses out-of-sample, the ranking was noise and every number
    # above it should be discarded rather than deployed.
    # ------------------------------------------------------------------
    rule("WALK-FORWARD CHECK (rank on the past, score on the future)")
    ordered = sorted(events, key=lambda e: (e.bar_index, e.asset))
    cut = int(len(ordered) * 0.7)
    train, test = ordered[:cut], ordered[cut:]
    print(f"  train {len(train)} events (earliest 70%)  |  "
          f"test {len(test)} events (latest 30%)")
    if len(train) >= 60 and len(test) >= 30:
        tr_rows = sweep_grid(train, SL_MULTIPLES, TP_RS, MAX_BARS, min_n=40)
        if not tr_rows:
            print("  train ranking empty")
        else:
            print(f"\n  {'rank':>4} {'SLxATR':>7}{'TP R':>7}{'bars':>6}"
                  f"{'trainR':>9}{'testR':>9}{'testWR%':>9}{'test n':>8}")
            for i, r in enumerate(tr_rows[:8], start=1):
                t = summarise_cell(test, r.sl_width, r.tp_r, r.max_bars)
                d = t.as_row()
                print(f"  {i:>4} {r.sl_width:>7.2f}{r.tp_r:>7.2f}{r.max_bars:>6}"
                      f"{r.mean_r:>+9.3f}{d['mean_r']:>+9.3f}"
                      f"{d['win_rate_pct']:>9.1f}{d['n']:>8}")
            best_tr = tr_rows[0]
            tst = summarise_cell(test, best_tr.sl_width, best_tr.tp_r, best_tr.max_bars)
            d = tst.as_row()
            verdict = ("HOLDS OUT" if d["mean_r"] > 0 else
                       "COLLAPSES out-of-sample -> the ranking was noise")
            print(f"\n  train winner {best_tr.sl_width:.2f}xATR / "
                  f"{best_tr.tp_r:.2f}R / {best_tr.max_bars} bars: "
                  f"train {best_tr.mean_r:+.3f}R -> test {d['mean_r']:+.3f}R")
            print(f"  VERDICT: {verdict}")
            live_t = summarise_cell(test, LIVE_SL_ATR_MULT, LIVE_RR, 20)
            ld = live_t.as_row()
            print(f"  production cell (1.5xATR / 2.0R / 20 bars) on the SAME "
                  f"test set: meanR={ld['mean_r']:+.3f} WR={ld['win_rate_pct']:.1f}%")
    else:
        print(f"  too few events for a holdout (train={len(train)}, "
              f"test={len(test)}) — read everything above as indicative only")

    rule("PER-ASSET WALK-FORWARD (does any pair's edge survive?)")
    if best and len(train) >= 40:
        print(f"  cell {best.sl_width:.2f}xATR / {best.tp_r:.2f}R / "
              f"{best.max_bars} bars")
        print(f"  {'asset':8}{'trainR':>9}{'trainWR%':>10}{'testR':>9}"
              f"{'testWR%':>9}{'test n':>8}{'':>4}")
        # Rank each pair on the TRAIN period ONLY, then apply that ranking to
        # the test period. Deciding "KEEP" by looking at the test numbers
        # would leak the holdout into the selection and make the final
        # figure look better than it is — the exact mistake this whole check
        # exists to catch.
        stats = []
        for asset in sorted(series):
            tr = [e for e in train if e.asset == asset]
            te = [e for e in test if e.asset == asset]
            if len(tr) < 8 or len(te) < 5:
                continue
            a = summarise_cell(tr, best.sl_width, best.tp_r, best.max_bars)
            stats.append((asset, a, te))
        stats.sort(key=lambda t: -t[1].mean_r)
        keep, drop = [], []
        for asset, a, te in stats:
            b = summarise_cell(te, best.sl_width, best.tp_r, best.max_bars)
            da, db = a.as_row(), b.as_row()
            flag = "KEEP" if a.mean_r > 0 else "drop"
            (keep if flag == "KEEP" else drop).append(asset)
            print(f"  {asset:8}{da['mean_r']:>+9.3f}{da['win_rate_pct']:>10.1f}"
                  f"{db['mean_r']:>+9.3f}{db['win_rate_pct']:>9.1f}"
                  f"{db['n']:>8}  {flag}")
        print(f"\n  selected on TRAIN alone — positive: {', '.join(keep) or 'none'}")
        print(f"  selected on TRAIN alone — negative: {', '.join(drop) or 'none'}")
        if keep:
            sub_tr = [e for e in train if e.asset in keep]
            sub_te = [e for e in test if e.asset in keep]
            a = summarise_cell(sub_tr, best.sl_width, best.tp_r, best.max_bars)
            b = summarise_cell(sub_te, best.sl_width, best.tp_r, best.max_bars)
            db = b.as_row()
            print(f"  restricted to the TRAIN-selected keepers: "
                  f"train {a.mean_r:+.3f}R -> test {b.mean_r:+.3f}R "
                  f"(test WR {db['win_rate_pct']:.1f}%, n={db['n']})")
            if drop:
                dr_tr = [e for e in train if e.asset in drop]
                dr_te = [e for e in test if e.asset in drop]
                x = summarise_cell(dr_tr, best.sl_width, best.tp_r, best.max_bars)
                y = summarise_cell(dr_te, best.sl_width, best.tp_r, best.max_bars)
                print(f"  the DROPPED pairs, for contrast: train {x.mean_r:+.3f}R "
                      f"-> test {y.mean_r:+.3f}R (test n={y.n})")
            print("  ^ still ONE selection across 9 pairs on ~40 train events "
                  "each — a hypothesis to test forward, not a result.")

    print("\nNo settings were changed. Treat the top cells as a proposal to test "
          "forward, not a conclusion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
