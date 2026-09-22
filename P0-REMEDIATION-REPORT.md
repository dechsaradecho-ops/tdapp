# P0 Remediation Report — Entry / Execution / Risk Control

Scope: 5 P0 Critical issues across the Entry → Execution → Risk-Control chain.
Constraints honoured: inspection-first, backward-compatible, per-fix automated tests,
full suite run, scenario matrix, no threshold tuning, no big rewrites, no silent risk
increase, Single-Path / Fail-Closed preserved. Migration added only where required.

Full backend suite at completion: **1002 passed, 0 failed (47s)**.

---

## Section 1 — Findings before any change (evidence-backed)

| P0 | Finding (as-found) | Location |
|----|--------------------|----------|
| **P0-1** | `opportunity_score(ind)` had **no `direction` parameter** and was structurally **long-biased**: trend `ema_fast>ema_slow → +20 else −5`; momentum branch driven only by `bull_trend`; `if macd_hist>0: +10` unconditional (no bearish mirror); `supertrend==1 and macd>0 → +5` long-only; breakout bonus only for upside breakouts. SELL setups were systematically under-scored and suppressed. | `engine/strategy_engine.py` |
| **P0-2** | `size_position` returned `max(lots, effective_min_lot(s, asset))` — the min-lot FLOOR silently **inflated risk above the per-trade budget** instead of blocking. `effective_sl_tp(..., apply_cap=…)` **tightened a structural stop** to `budget/(min_lot·contract)` to force a min-lot fill. | `services/execution.py`, `models/schemas.py` |
| **P0-3** | `limit_expand` timeout path (`settle_expired`) **auto-applied +5pp on lapse** when `kill_expand_auto_apply=True` (default) — a SILENT risk increase with no human confirm. | `services/limit_expand.py` |
| **P0-4** | **No configuration validation.** A self-contradictory `trading_settings` row (`risk_per_trade_pct > kill_daily_loss_pct`) was undetected; one trade at full size could blow the whole day's loss budget before the circuit breaker saw it. | (missing) `core/config_validation.py` |
| **P0-5** | `execute_signal` re-anchored the ENTRY to live spot but **never re-checked the REASON the signal existed.** A signal up to `SIGNAL_TTL_MIN` (30) min old could fire after its trend / Supertrend / XAU breakout structure / news state had inverted. | `services/execution.py` |

All three consumers (manual `/approve`, `auto_trader`, `/trading/extended-open`) route
through the single `execution.execute_signal` — the natural seam for every fix.

---

## Section 2 — Root cause per P0

- **P0-1** — The scorer was a long-only function that both BUY and SELL proposals reused;
  direction was resolved *after* scoring, so a bearish setup could never mirror a bullish one.
- **P0-2** — Sizing conflated "make the order openable" with "respect the risk budget":
  the floor was applied with `max()`, and the cap *moved the stop* instead of refusing.
- **P0-3** — Expiry-plus-auto-apply turned a *timeout* (no human decision) into a silent
  limit widening — the opposite of fail-closed.
- **P0-4** — No validation layer existed; settings were trusted verbatim.
- **P0-5** — Execution trusted a stale signal's thesis; only the *price* was refreshed.

---

## Section 3 — Files changed

New:
- `backend/app/core/thesis_validation.py` (P0-5)
- `backend/tests/test_thesis_validation.py` (P0-5)
- `backend/app/core/config_validation.py` (P0-4, prior)
- `backend/tests/test_config_validation.py` (P0-4, prior)
- `database/043_kill_expand_auto_widen.sql` (P0-3, prior)
- `STRATEGY.md`

Modified (this P0-5 step): `backend/app/services/execution.py`,
`backend/app/models/schemas.py`, `backend/app/api/routes/signals.py`,
`backend/app/workers/auto_trader.py`, `backend/tests/test_auto_trader.py`,
`backend/tests/test_api_routes.py`.
Plus all P0-1…P0-4 files per `git diff --stat` (19 files, +753 / −96).

---

## Section 4 — Behaviour before → after

| Area | Before | After |
|------|--------|-------|
| **P0-1** score | Long-biased; SELL capped low | `opportunity_score(ind, direction)` symmetric per side; `build_proposal` pushes direction through |
| **P0-2** sizing | `max(lots, min_lot)` inflates risk; stop tightened to fit | Block with `minimum_lot_exceeds_risk_budget` when floor > budget; never widen risk |
| **P0-3** limit-expand lapse | Auto-apply +5pp on timeout | Keep original limits; require explicit confirm (fail-closed); `kill_expand_auto_widen` opt-in |
| **P0-4** config | Trusted verbatim | `validate_settings` → INVALID/UNKNOWN **blocks** with `configuration_error` |
| **P0-5** thesis | Fires on stale thesis | Fresh snapshot re-check → **blocks** with `thesis_error`; missing snapshot = FAIL-CLOSED |

---

## Section 5 — Tests added

- **P0-1**: direction-aware scoring tests (bullish/bearish parity) — in `test_engines`.
- **P0-2**: min-lot budget-breach tests (`test_auto_trader`).
- **P0-3**: `test_limit_expand.py` (78 tests) — timeout keeps limits, fail-closed.
- **P0-4**: `test_config_validation.py` (18 tests) + seam test in `execute_signal`.
- **P0-5**: `test_thesis_validation.py` (32 tests) — every reject code, the `execute_signal`
  seam (never reaches broker), fail-closed on missing snapshot, and **manual == auto parity**
  (`@pytest.mark.parametrize("source", ["approved", "auto"])`).

---

## Section 6 — Test results

```
pytest tests/ -q
1002 passed, 0 failed  (47s)
└─ test_thesis_validation.py        32 passed
└─ test_config_validation.py        18 passed
└─ test_limit_expand.py             78 passed
```

Pre-existing failures (`test_time_stop_hits_aged_position_even_with_sl`,
`test_deferred_time_stop_fires_on_first_open_cycle`) root-caused to **trading-day** position
age (weekends subtracted by `market_open_days_between`; 6 calendar days = 4.0 trading days <
`max_hold_days=5`) and fixed date-independently (fixture age 6 → 10 calendar days).

---

## Section 7 — Scenario matrix (A–L)

Defaults: `capital=10_000`, `risk_per_trade_pct=1.0` ⇒ risk budget **$100/trade**;
`min_confidence=70`; `max_open_positions=4`. Risk $ = lot × stop-distance × contract
(FX 1 lot = 100 000 units; XAU 1 lot = 100 oz).

| # | Scenario | Dir | Conf/Opp | Gate | Reject reason | Intended→Final SL | Lot | Risk $ | Risk % | Result |
|---|----------|-----|----------|------|---------------|-------------------|-----|--------|--------|--------|
| A | BUY, bullish snapshot (EMA↑, ST↑, adx 30) | BUY | 80/80 | pass | — | structural | sized | ≈100 | ≈1.0% | **ALLOWED** |
| B | SELL, bearish snapshot (EMA↓, ST↓) | SELL | 80/80 | pass | — | structural | sized | ≈100 | ≈1.0% | **ALLOWED** |
| C | BUY vs bearish snapshot (EMA↓/ST↓) | BUY | 80/80 | thesis | `trend_flipped`+`supertrend_flipped` | n/a | 0 | 0 | 0 | **BLOCKED** |
| D | SELL vs bullish snapshot (EMA↑/ST↑) | SELL | 80/80 | thesis | `trend_flipped`+`supertrend_flipped` | n/a | 0 | 0 | 0 | **BLOCKED** |
| E | FX, min-lot affordable (stop ≈100 pip, min 0.01) | BUY | 80/80 | pass | — | structural | ≥0.01 | ≤100 | ≤1.0% | **ALLOWED** |
| F | FX, min-lot exceeds budget (stop ≈1000 pip) | BUY | 80/80 | P0-2 | `minimum_lot_exceeds_risk_budget` | structural | 0 | >100 | >1.0% | **BLOCKED** |
| G | XAU structural stop affordable | BUY | 80/80 | pass | — | structural (breakout valid) | sized | ≈100 | ≈1.0% | **ALLOWED** |
| H | XAU min-lot exceeds budget | BUY | 80/80 | P0-2 | `minimum_lot_exceeds_risk_budget` | structural | 0 | >100 | >1.0% | **BLOCKED** |
| I | Stale signal, thesis changed (ST flipped) | BUY | 80/80 | thesis | `supertrend_flipped` | n/a | 0 | 0 | 0 | **BLOCKED** |
| J | XAU signal, breakout structure gone | BUY | 80/80 | thesis | `breakout_invalidated` | n/a | 0 | 0 | 0 | **BLOCKED** |
| K | DD breach, no human response | — | — | time-stop | `signal_expired` / hold | original | 0 | 0 | 0 | **BLOCKED (keep limits)** |
| L | DD breach + explicit approve | — | — | pass (confirmed) | — | structural | sized | ≤100 | ≤1.0% | **ALLOWED (confirmed)** |

Invariant: **manual approve and auto-trader produce the SAME row** (both call
`execute_signal`; `test_thesis_validation` parametrises `source ∈ {approved, auto}`).

---

## Section 8 — Migrations

- Latest before task start: **042**. Added in this remediation: **043**
  (`043_kill_expand_auto_widen.sql`) — adds the `kill_expand_auto_widen` column used by
  P0-3's opt-in auto-widen (defaults **false** = fail-closed). No old migration modified.
  P0-5 required **no** schema change (the `signals` row already carries `direction`,
  `created_at`; the fresh thesis is re-derived from the live feed at execution time).

---

## Section 9 — Docs updated

- `FEATURES.md` — P0-3 auto-widen setting surfaced.
- `STRATEGY.md` — signal TTL (30 min), single-path execution, fail-closed rules.
- Frontend: `settings/page.tsx`, `logs/page.tsx`, `LimitExpandPopup.tsx`, `lib/types.ts`
  expose the P0-3 `kill_expand_auto_widen` toggle (fail-closed default).

---

## Section 10 — Remaining risks / P1

- **P0-5 freshness window**: validation runs at execution time; between the snapshot fetch
  and the broker fill there is sub-second drift (unavoidable without a market-order lock).
  The 60 s quote cache bounds how *new* the snapshot is (can be up to 60 s old) — acceptable
  vs. the previous 30 min, but a future P1 could force a cache-bypass fetch here.
- **`breakout_invalidated` is XAUUSD-only** (the feed only computes `breakout_state` for gold);
  other assets rely on trend/Supertrend/regime checks.
- **Regime thresholds** (`adx<20` sideway, `atr_pct>2.5` high-vol) are read from the same
  engine constants — not tuned here per the no-threshold-tuning constraint, but they are the
  knobs a P1 review could revisit.
- **P0-1** scoring parity is covered by tests but has no production-metric dashboard yet
  (SELL/BUY signal-count ratio would be the observable to watch).
