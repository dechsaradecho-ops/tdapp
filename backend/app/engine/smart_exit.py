"""SMART EXIT ENGINE — continuous AI exit evaluation for open positions.

Purpose (spec): Entry Signal สำคัญเท่ากับ Exit Signal — every open position
must pass this engine every guard cycle before the hold/close decision, and
the reason must ALWAYS be shown (NO POSITION LEFT BEHIND).

EXIT PRIORITY (spec order):
  1. Emergency Exit (kill switch engaged)
  2. Stop Loss (handled by position_guard — SL hit check first)
  3. Take Profit (handled by position_guard — TP hit check first)
  4. Trailing Stop (R-ladder 1R→BE / 2R→+1R / 3R→+2R + ATR trail)
  5. AI Exit Score (hold-quality 0-100 from 9 factors)
  6. Trend Reversal (EMA cross / ADX<20 / momentum negative / opp<50)
  7. Time Stop (max_hold_days — handled by position_guard)
  8. News Exit (high-impact event approaching + profit to protect)

This module is PURE (no DB, no network, no broker) so it stays unit-testable.
The guard (position_guard.py) supplies: position, live price, age, R-multiple,
indicator snapshot dict (from quotes.fetch_all_snapshots, 60s cached),
news status string, avg holding days, drawdown pct, and AppSettings.

Score semantics: exit_score = HOLD QUALITY 0-100 (higher = safer to hold).
  >= 65 → High, 45-65 → Medium, < 45 → Low.
Example from spec: 4 Days / +1.8R / 42 / Low / Close → 42 < 45 → Low → CLOSE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Recommendation = Literal[
    "HOLD", "MOVE_SL", "PARTIAL_25", "PARTIAL_50", "CLOSE", "EMERGENCY_CLOSE",
]

FinalAction = Literal[
    "CONTINUE", "PROTECT", "SCALE_OUT", "CLOSE", "EMERGENCY_CLOSE",
]


@dataclass
class ExitFactorScores:
    """9 factor breakdown shown in the monitor popup (0-100 each)."""
    trend_strength: float = 50.0
    momentum: float = 50.0
    volume_proxy: float = 50.0
    market_regime: float = 50.0
    news_risk: float = 50.0
    holding_time: float = 50.0
    volatility: float = 50.0
    opportunity_score: float = 50.0
    risk_exposure: float = 50.0


@dataclass
class ExitSignals:
    tp_hit: bool = False
    sl_hit: bool = False
    trailing: bool = False
    reversal: bool = False
    news: bool = False
    time_stop: bool = False


@dataclass
class ExitDecision:
    """Full EXIT DECISION OUTPUT per spec."""
    position_age_days: float = 0.0
    r_multiple: float = 0.0
    exit_score: float = 50.0          # hold quality 0-100
    quality: Literal["High", "Medium", "Low"] = "Medium"
    factors: ExitFactorScores = field(default_factory=ExitFactorScores)
    signals: ExitSignals = field(default_factory=ExitSignals)
    recommendation: Recommendation = "HOLD"
    final: FinalAction = "CONTINUE"
    reasoning: list[str] = field(default_factory=list)
    # Machine-readable trigger tags for the journal reason prefix
    # e.g. "exit_score", "reversal", "news", "volatility", "left_behind",
    # "profit_protect", "time_stop", "emergency"
    trigger: str = ""
    # Effective age threshold (days) the left_behind rule used this cycle.
    # 0.0 when the rule is disabled (no_behind_hold_mult <= 0). Surfaced so the
    # monitor can show the SAME number the engine reasons about instead of
    # re-deriving it in the UI.
    behind_days: float = 0.0


def quality_of(score: float) -> Literal["High", "Medium", "Low"]:
    if score >= 65:
        return "High"
    if score >= 45:
        return "Medium"
    return "Low"


def _clamp01(x: float) -> float:
    return max(0.0, min(100.0, x))


def _dir_sign(direction: str) -> int:
    return 1 if str(direction or "").upper() == "BUY" else -1


def left_behind_days(*, settings, avg_hold_days: float | None,
                     max_hold_days: int | None = None) -> float:
    """Age threshold (days) of the NO-POSITION-LEFT-BEHIND rule; 0 = disabled.

    SINGLE definition — `evaluate_exit`, the monitor snapshot (popup text) and
    tests all read this, so the number the UI shows is the number the engine
    used. Computed as:

        days = max(avg_hold × no_behind_hold_mult, no_behind_min_days)
        days = min(days, max_hold_days)      # only when the time stop is on

    The floor matters MORE after the multiplier was lowered (1.75): the
    average is itself floored at 0.5 day, so a collapsed sample would give a
    0.9-day threshold — the 2026-09-11 incident was exactly a collapsed
    average. The cap keeps the rule from ever drifting PAST the time stop,
    which is what made it dead code (mult=5 → 11.9 days vs a 5-day stop).

    Never raises; returns 0.0 when the rule is off (mult <= 0).
    """
    try:
        mult = float(getattr(settings, "no_behind_hold_mult", 1.75) or 0)
    except (TypeError, ValueError):
        mult = 0.0
    if mult <= 0:
        return 0.0
    try:
        avg = max(0.5, float(avg_hold_days or 4.0))
    except (TypeError, ValueError):
        avg = 4.0
    days = avg * mult
    try:
        floor = float(getattr(settings, "no_behind_min_days", 2.0) or 0)
    except (TypeError, ValueError):
        floor = 0.0
    if floor > 0:
        days = max(days, floor)
    hard = max_hold_days if max_hold_days is not None else getattr(
        settings, "max_hold_days", 0)
    try:
        hard = int(hard or 0)
    except (TypeError, ValueError):
        hard = 0
    if hard > 0:
        days = min(days, float(hard))
    return days


def evaluate_exit(
    *,
    asset: str,
    direction: str,
    entry_price: float,
    price: float,
    stop_loss: Optional[float],
    age_days: float,
    settings: Any,
    snapshot: Optional[dict] = None,
    news_status: str = "SAFE",
    news_event: str = "",
    avg_hold_days: float = 4.0,
    drawdown_pct: float = 0.0,
    opportunity_score: Optional[float] = None,
) -> ExitDecision:
    """Pure exit evaluation for ONE position. Never raises (fail-safe HOLD)."""
    try:
        return _evaluate(
            asset=asset, direction=direction, entry_price=entry_price,
            price=price, stop_loss=stop_loss, age_days=age_days,
            settings=settings, snapshot=snapshot or {},
            news_status=news_status, news_event=news_event,
            avg_hold_days=avg_hold_days, drawdown_pct=drawdown_pct,
            opportunity_score=opportunity_score,
        )
    except Exception:
        return ExitDecision(reasoning=["ประเมินไม่สำเร็จ — ถือต่อ (fail-safe HOLD)"])


def _evaluate(
    *,
    asset: str,
    direction: str,
    entry_price: float,
    price: float,
    stop_loss: Optional[float],
    age_days: float,
    settings: Any,
    snapshot: dict,
    news_status: str,
    news_event: str,
    avg_hold_days: float,
    drawdown_pct: float,
    opportunity_score: Optional[float],
) -> ExitDecision:
    sign = _dir_sign(direction)
    s = settings
    snap = snapshot or {}

    # ---- R-multiple -------------------------------------------------------
    r_dist = abs(entry_price - (stop_loss or entry_price))
    if r_dist <= 0:
        # No SL → fall back to 1% of price so R stays meaningful
        r_dist = abs(entry_price) * 0.01 or 1e-9
    r_mult = ((price - entry_price) * sign) / r_dist

    max_hold = int(getattr(s, "max_hold_days", 5) or 0)
    exit_close = float(getattr(s, "exit_score_close", 45.0) or 45.0)
    protect_r = float(getattr(s, "profit_protect_r", 2.0) or 0)
    reversal_opp = float(getattr(s, "reversal_opp_min", 50.0) or 50.0)
    news_on = bool(getattr(s, "news_exit_enabled", True))
    news_min_r = float(getattr(s, "news_exit_min_r", 1.0) or 0)
    vol_exit_atr = float(getattr(s, "volatility_exit_atr", 2.5) or 0)
    behind_min_r = float(getattr(s, "no_behind_min_r", 0.5) or 0)
    behind_mult = float(getattr(s, "no_behind_hold_mult", 1.75) or 0)

    ema_fast = float(snap.get("ema_fast") or 0)
    ema_slow = float(snap.get("ema_slow") or 0)
    adx = float(snap.get("adx") or 0)
    st_dir = int(snap.get("supertrend_dir") or 0)
    rsi = float(snap.get("rsi") if snap.get("rsi") is not None else 50.0)
    macd = float(snap.get("macd_hist") or 0)
    atr_pct = float(snap.get("atr_pct") or 0)
    vol_idx = float(snap.get("volatility_index") or 0)
    opp = float(opportunity_score if opportunity_score is not None
                else snap.get("opportunity_score") or 50.0)

    has_trend_data = bool(ema_fast and ema_slow)

    # ---- 9 factors (0-100, higher = safer to HOLD) ------------------------
    # 1. Trend Strength — does the trend still favor the position?
    if has_trend_data:
        aligned = (ema_fast > ema_slow) if sign == 1 else (ema_fast < ema_slow)
        st_aligned = (st_dir == sign) if st_dir else True
        if aligned and st_aligned and adx >= 25:
            trend = 90.0
        elif aligned and adx >= 20:
            trend = 70.0
        elif aligned:
            trend = 55.0
        elif adx < 20:
            trend = 35.0
        else:
            trend = 20.0
    else:
        trend = 50.0

    # 2. Momentum — RSI + MACD aligned with direction?
    mom = 50.0
    if sign == 1:
        if rsi >= 55 and macd > 0:
            mom = 80.0
        elif rsi >= 45 and macd >= 0:
            mom = 65.0
        elif rsi < 40 or macd < 0:
            mom = 30.0
        elif rsi > 75:
            mom = 45.0  # overbought — fade risk
    else:
        if rsi <= 45 and macd < 0:
            mom = 80.0
        elif rsi <= 55 and macd <= 0:
            mom = 65.0
        elif rsi > 60 or macd > 0:
            mom = 30.0
        elif rsi < 25:
            mom = 45.0  # oversold — bounce risk

    # 3. Volume proxy — no volume in Candle/quotes; volatility_index stands in
    # (moderate activity = healthy, dead or explosive = unhealthy).
    if vol_idx <= 0:
        volume = 50.0
    elif 3.0 <= vol_idx <= 12.0:
        volume = 75.0
    elif vol_idx < 3.0:
        volume = 45.0
    elif vol_idx <= 20.0:
        volume = 40.0
    else:
        volume = 25.0

    # 4. Market Regime — aligned trend regime holds, chop/news doesn't.
    regime = str(snap.get("regime") or "").lower()
    if not regime:
        # derive from ADX/ATR like strategy_engine.regime_of
        if adx < 20:
            regime = "sideway"
        elif atr_pct > 2.5:
            regime = "high_volatility"
        elif has_trend_data and ((ema_fast > ema_slow) == (sign == 1)):
            regime = "bull_trend" if sign == 1 else "bear_trend"
        else:
            regime = "sideway"
    if regime in ("bull_trend", "strong_bull_trend") and sign == 1:
        regime_score = 85.0
    elif regime in ("bear_trend", "strong_bear_trend") and sign == -1:
        regime_score = 85.0
    elif regime in ("sideway",):
        regime_score = 35.0
    elif regime in ("high_volatility", "news_driven_market"):
        regime_score = 25.0
    else:
        regime_score = 30.0  # counter-trend regime

    # 5. News Risk
    ns = str(news_status or "SAFE").upper()
    news_score = {"SAFE": 85.0, "CAUTION": 55.0, "DANGER": 20.0}.get(ns, 60.0)

    # 6. Holding Time — fresh holds, near-limit decays.
    if max_hold > 0:
        frac = age_days / max_hold
        holding = 85.0 if frac < 0.4 else 65.0 if frac < 0.7 else 40.0 if frac < 1.0 else 15.0
    else:
        holding = 70.0 if age_days < 10 else 50.0 if age_days < 20 else 30.0

    # 7. Volatility — moderate ATR holds, spike threatens.
    if atr_pct <= 0:
        volatility = 50.0
    elif 0.4 <= atr_pct <= 1.5:
        volatility = 80.0
    elif atr_pct <= 2.5:
        volatility = 55.0
    else:
        volatility = 25.0

    # 8. Opportunity Score — fresh edge still there?
    opportunity = _clamp01(opp)

    # 9. Risk Exposure — portfolio drawdown pressure.
    if drawdown_pct <= 0:
        exposure = 85.0
    elif drawdown_pct < 2:
        exposure = 65.0
    elif drawdown_pct < 5:
        exposure = 40.0
    else:
        exposure = 20.0

    factors = ExitFactorScores(
        trend_strength=trend, momentum=mom, volume_proxy=volume,
        market_regime=regime_score, news_risk=news_score,
        holding_time=holding, volatility=volatility,
        opportunity_score=opportunity, risk_exposure=exposure,
    )

    # Weighted hold-quality score
    score = (
        trend * 0.20 + mom * 0.15 + regime_score * 0.15 + opportunity * 0.10
        + news_score * 0.10 + holding * 0.10 + volatility * 0.10
        + volume * 0.05 + exposure * 0.05
    )
    score = round(_clamp01(score), 1)
    quality = quality_of(score)

    reasoning: list[str] = []
    if has_trend_data:
        reasoning.append(
            f"เทรนด์ {'หนุน' if (trend >= 55) else 'ต้าน'}ไม้ "
            f"(EMA50 {ema_fast:g} vs EMA200 {ema_slow:g}, ADX {adx:.0f})")
    if mom < 45:
        reasoning.append(
            f"โมเมนตัมอ่อนแรง ({'RSI' if True else ''} {rsi:.0f}, MACD {macd:+.2f}) — เสี่ยงกลับตัว")
    if regime_score < 45:
        reasoning.append(f"สภาวะตลาดไม่เอื้อ ({regime}) — ควรลดเสี่ยง")
    if ns != "SAFE":
        reasoning.append(f"ข่าว {news_event or 'impact สูง'} ใกล้ตัว ({ns}) — ผันผวนคาดเดาไม่ได้")
    if max_hold > 0 and age_days >= max_hold * 0.7:
        reasoning.append(f"ถือมา {age_days:.1f} วัน ใกล้/เกินกรอบ {max_hold} วัน")
    if atr_pct > 2.5:
        reasoning.append(f"ผันผวนสูง (ATR {atr_pct:.2f}%) — เสี่ยง spike")
    if opportunity < reversal_opp:
        reasoning.append(f"Opportunity Score เหลือ {opportunity:.0f} (< {reversal_opp:g}) — edge หาย")

    signals = ExitSignals()
    if max_hold > 0 and age_days >= max_hold:
        signals.time_stop = True

    # ---- Trend Reversal detection (spec: BUY closes on these) -------------
    reversal_votes = 0
    if has_trend_data:
        against = (ema_fast < ema_slow) if sign == 1 else (ema_fast > ema_slow)
        if against:
            reversal_votes += 1
            reasoning.append(
                f"กลับตัว: EMA50 {'ต่ำกว่า' if sign == 1 else 'สูงกว่า'} EMA200")
    if 0 < adx < 20:
        reversal_votes += 1
        reasoning.append(f"กลับตัว: ADX {adx:.0f} < 20 — เทรนด์ตาย")
    momentum_negative = (macd < 0) if sign == 1 else (macd > 0)
    if momentum_negative:
        reversal_votes += 1
        reasoning.append("กลับตัว: Momentum เปลี่ยนขั้ว (MACD ตัดฝั่งตรงข้าม)")
    if opportunity < reversal_opp:
        reversal_votes += 1
    is_reversal = reversal_votes >= 2
    signals.reversal = bool(is_reversal)

    if ns == "DANGER":
        signals.news = True

    # ---- NO POSITION LEFT BEHIND ------------------------------------------
    avg_hold = max(0.5, float(avg_hold_days or 4.0))
    behind_days = left_behind_days(settings=s, avg_hold_days=avg_hold,
                                   max_hold_days=max_hold)
    left_behind = (
        behind_days > 0 and behind_min_r >= 0 and r_mult < behind_min_r
        and age_days > behind_days
    )

    volatility_spike = vol_exit_atr > 0 and atr_pct > vol_exit_atr
    news_exit = (
        news_on and ns == "DANGER" and news_min_r >= 0 and r_mult >= news_min_r
    )

    # ---- Recommendation (priority: emergency handled by caller) ------------
    rec: Recommendation = "HOLD"
    final: FinalAction = "CONTINUE"
    trigger = ""

    if left_behind:
        rec, final, trigger = "CLOSE", "CLOSE", "left_behind"
        reasoning.append(
            f"ไม้ค้างทุน: กำไร {r_mult:+.1f}R < {behind_min_r:g}R "
            f"+ ถือ {age_days:.1f} วัน เกินเกณฑ์ {behind_days:.1f} วัน "
            f"({behind_mult:g}× ค่าเฉลี่ย {avg_hold:.1f} วัน) "
            "— Capital Efficiency")
    elif news_exit:
        rec, final, trigger = "CLOSE", "CLOSE", "news"
        reasoning.append(
            f"News Exit: กำไร {r_mult:+.1f}R + ข่าว {news_event or 'impact สูง'} ใกล้ตัว "
            "— ปิดก่อน spike")
    elif is_reversal:
        rec, final, trigger = "CLOSE", "CLOSE", "reversal"
        reasoning.append(f"Trend Reversal ({reversal_votes}/4 สัญญาณ) — ปิดก่อนขาดทุนเพิ่ม")
    elif score < exit_close:
        # Deep loss of edge → full close; modest profit + weak hold → scale out
        if r_mult >= 1.0:
            rec, final, trigger = "PARTIAL_50", "SCALE_OUT", "exit_score"
            reasoning.append(
                f"Exit Score {score:.0f} ({quality}) ต่ำกว่า {exit_close:g} "
                f"แต่กำไร {r_mult:+.1f}R — แบ่งปิด 50% ล็อกกำไร")
        else:
            rec, final, trigger = "CLOSE", "CLOSE", "exit_score"
            reasoning.append(
                f"Exit Score {score:.0f} ({quality}) ต่ำกว่า {exit_close:g} — ปิดทั้งไม้")
    elif volatility_spike and r_mult >= 1.0:
        rec, final, trigger = "PARTIAL_50", "SCALE_OUT", "volatility"
        reasoning.append(
            f"Volatility Exit: ATR {atr_pct:.2f}% > {vol_exit_atr:g}% "
            f"+ กำไร {r_mult:+.1f}R — แบ่งปิด 50% ลดเสี่ยง")
    elif volatility_spike:
        rec, final, trigger = "PARTIAL_25", "SCALE_OUT", "volatility"
        reasoning.append(
            f"Volatility Exit: ATR {atr_pct:.2f}% > {vol_exit_atr:g}% — แบ่งปิด 25%")
    elif protect_r > 0 and r_mult >= protect_r and quality != "High":
        rec, final, trigger = "PARTIAL_50", "SCALE_OUT", "profit_protect"
        reasoning.append(
            f"Profit Protection: กำไร {r_mult:+.1f}R ≥ {protect_r:g}R "
            f"แต่คุณภาพ {quality} — แบ่งปิด 50% กันกำไรหาย")
    elif protect_r > 0 and r_mult >= 1.0 and quality == "Low":
        rec, final, trigger = "MOVE_SL", "PROTECT", "profit_protect"
        reasoning.append(
            f"กันกำไร: {r_mult:+.1f}R แต่คุณภาพ {quality} — ขยับ SL ป้องกัน")
    else:
        reasoning.append(
            f"ถือต่อ: Score {score:.0f} ({quality}), {r_mult:+.1f}R, "
            f"อายุ {age_days:.1f} วัน — ไม่มีสัญญาณออก")

    if not reasoning:
        reasoning = ["ถือต่อ — ไม่มีสัญญาณออก"]

    return ExitDecision(
        position_age_days=round(float(age_days), 2),
        r_multiple=round(float(r_mult), 2),
        exit_score=score, quality=quality, factors=factors,
        signals=signals, recommendation=rec, final=final,
        reasoning=reasoning[:5], trigger=trigger,
        behind_days=round(float(behind_days), 2),
    )


def ladder_sl(
    *, entry_price: float, direction: str, r_distance: float,
    r_multiple: float,
) -> Optional[float]:
    """R-ladder floor for the trailing stop (priority 4).

    >= 1R → breakeven, >= 2R → +1R, >= 3R → +2R. Returns None below 1R.
    """
    if r_distance <= 0 or r_multiple < 1.0:
        return None
    sign = _dir_sign(direction)
    if r_multiple >= 3.0:
        lock_r = 2.0
    elif r_multiple >= 2.0:
        lock_r = 1.0
    else:
        lock_r = 0.0
    return round(entry_price + sign * lock_r * r_distance, 5)
