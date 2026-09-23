"""AI Strategy Engine — combines trend, momentum, volatility, news and sentiment inputs
into an opportunity score (0-100) and an explainable trading proposal.

Indicator inputs (EMA, ADX, Supertrend, RSI, MACD, ATR, ...) are supplied by the
Market Scanner worker; this engine only scores/explains, keeping it testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.models.schemas import (
    AssetOpportunity,
    FinalDecision,
    LimitLevel,
    OpportunityBand,
    RiskProfile,
    SLTPLevel,
    SignalProposal,
)


@dataclass
class IndicatorSnapshot:
    """Values computed upstream (worker #1 / data feed) for one asset."""

    asset: str
    price: float

    # Trend
    ema_fast: float = 0.0          # e.g. EMA50
    ema_slow: float = 0.0          # e.g. EMA200
    adx: float = 0.0               # >25 = trending
    supertrend_dir: int = 0        # +1 up / -1 down

    # Momentum
    rsi: float = 50.0              # 0-100
    macd_hist: float = 0.0         # >0 bullish
    price_change_pct_20: float = 0.0

    # Volatility
    atr_pct: float = 0.0           # ATR / price * 100
    volatility_index: float = 0.0  # e.g. GVZ for gold, VIX for indices

    # News / sentiment (from worker #2)
    news_sentiment: float = 0.0    # -1 .. +1
    high_impact_event: bool = False

    # Breakout-retest context (strategy D — gold only, 0.0 elsewhere).
    # breakout_state: 2 = breakout (close above prior 20-bar high),
    # 1 = retest (dipped back to the level, closed above it), 0 = none.
    # breakout_level: the broken high — SL invalidation reference.
    breakout_state: float = 0.0
    breakout_level: float = 0.0

    source: str = "demo"           # "live" (quote feed) / "demo" (random-walk fallback)

    reasons: list[str] = field(default_factory=list)


BANDS = ((81.0, 100.0, OpportunityBand.very_high), (61.0, 81.0, OpportunityBand.high),
         (31.0, 61.0, OpportunityBand.medium), (0.0, 31.0, OpportunityBand.low))


def regime_of(ind: IndicatorSnapshot) -> str:
    """Classify the market regime from an indicator snapshot (worker + API share this)."""
    if ind.high_impact_event:
        return "news_driven_market"
    if ind.adx < 20:
        return "sideway"
    if ind.atr_pct > 2.5:
        return "high_volatility"
    if ind.ema_fast > ind.ema_slow:
        return "bull_trend" if ind.adx < 35 else "strong_bull_trend"
    return "bear_trend" if ind.adx < 35 else "strong_bear_trend"


class StrategyEngine:
    """Deterministic scoring core + explainability. AI layer wraps this for narrative."""

    # ------------------------------------------------------------------
    def opportunity_score(self, ind: IndicatorSnapshot,
                          direction: Optional[str] = None) -> AssetOpportunity:
        """Score a setup 0-100. ``direction`` makes the score DIRECTION-AWARE.

        P0-1 (2026): the original scorer had NO direction parameter and was
        structurally long-biased — the EMA bonus was +20 for an uptrend and
        only −5 for a downtrend, ``macd_hist > 0`` always added +10 with no
        bearish counterpart, and ``supertrend_dir == 1 and macd_hist > 0`` was
        a long-only confirmation. The SAME long-biased score then fed both BUY
        and SELL proposals (direction was picked later in ``build_proposal``),
        so a bearish setup could never score as high as the mirror-image
        bullish one and SELL signals were systematically suppressed.

        ``direction``:
          * ``"BUY"`` / ``"SELL"`` — score THAT side (the trend/EMA/MACD/
            Supertrend/breakout components are graded relative to it);
          * ``None`` (default) — legacy auto-detect: derive the side from the
            trend (EMA, else Supertrend) exactly as before, so every existing
            caller/test keeps its behaviour AND the score is now graded with
            the same symmetric math instead of the old long-only bias.
        """
        want = self._resolve_direction(ind, direction)
        score = 0.0
        reasons: list[str] = []

        # --- Trend component (0-35) ---
        # Symmetric: the trend-agreement bonus is the same for BUY-in-uptrend
        # and SELL-in-downtrend; a trend that CONTRADICTS the wanted side is a
        # penalty, not a free −5 (old code only ever penalised the trend the
        # long-biased side did NOT want).
        trend_up: Optional[bool] = None
        if ind.ema_fast and ind.ema_slow:
            trend_up = ind.ema_fast > ind.ema_slow
            if trend_up == want:
                score += 20
                reasons.append("EMA50 สูงกว่า EMA200 → แนวโน้มขาขึ้น"
                               if trend_up else
                               "EMA50 ต่ำกว่า EMA200 → แนวโน้มขาลง")
            else:
                score -= 5
                reasons.append("EMA50 สูงกว่า EMA200 → แนวโน้มขาขึ้น สวนทางกับฝั่งที่ต้องการ"
                               if trend_up else
                               "EMA50 ต่ำกว่า EMA200 → แนวโน้มขาลง สวนทางกับฝั่งที่ต้องการ")
        if ind.adx >= 25:
            score += 15
            reasons.append(f"ADX = {ind.adx:.0f} (≥25) → มีความแข็งแรงของเทรนด์")
        elif ind.adx > 0:
            score += 5
            reasons.append(f"ADX = {ind.adx:.0f} (<25) → เทรนด์อ่อน/ไซด์เวย์")

        # --- Momentum (0-25): pullback-in-trend beats chase ---
        # สถิติ prod ชี้ว่าการไล่ราคา (chase) ที่ RSI สูงแล้วแพงกว่าการรอจังหวะย่อ
        # → ให้น้ำหนัก "pullback ในเทรนด์" มากกว่า "โมเมนตัมร้อนแรง"
        # Graded for the WANTED side: BUY wants an up Supertrend, SELL a down one.
        if want:   # BUY
            if 40 <= ind.rsi <= 55 and ind.supertrend_dir > 0:
                score += 15
                reasons.append(
                    f"RSI = {ind.rsi:.0f} + Supertrend ขาขึ้น → จังหวะ pullback "
                    "(ราคาย่อในเทรนด์ขาขึ้น) จุดเข้าดีกว่าการไล่ราคา")
            elif 55 < ind.rsi <= 70:
                score += 5
                reasons.append(
                    f"RSI = {ind.rsi:.0f} → โมเมนตัมร้อนแรงไปแล้ว เข้าตอนนี้คือการไล่ราคา (chase)")
            elif ind.rsi > 70:
                score += 5
                reasons.append(f"RSI = {ind.rsi:.0f} → overbought เสี่ยงย่อ")
            elif ind.rsi < 40 and ind.supertrend_dir > 0:
                score += 8
                reasons.append(
                    f"RSI = {ind.rsi:.0f} ย่อลึก แต่ Supertrend ยังขาขึ้น → รอสัญญาณกลับตัวก่อนเข้า")
        else:       # SELL — mirror of the BUY branch
            if 45 <= ind.rsi <= 60 and ind.supertrend_dir < 0:
                score += 15
                reasons.append(
                    f"RSI = {ind.rsi:.0f} + Supertrend ขาลง → จังหวะ rally ขึ้นในเทรนด์ขาลง "
                    "จุดขายดีกว่าการไล่เทลอง")
            elif 30 <= ind.rsi < 45:
                score += 5
                reasons.append(f"RSI = {ind.rsi:.0f} → โมเมนตัมขาลง ระวัง oversold ลึกเกิน")
            elif ind.rsi > 60 and ind.supertrend_dir < 0:
                score += 8
                reasons.append(
                    f"RSI = {ind.rsi:.0f} rally สูงแต่ Supertrend ยังขาลง → จุดขายแต่เสี่ยงสูง")
            elif ind.rsi < 30:
                score += 5
                reasons.append(f"RSI = {ind.rsi:.0f} → oversold เสี่ยง bounce แรง")
        # MACD now SYMMETRIC: a histogram that agrees with the wanted side
        # adds +10, one that contradicts it subtracts 10. Old code only ever
        # rewarded ``macd_hist > 0``, which made the metric long-only.
        if ind.macd_hist != 0:
            agrees = (ind.macd_hist > 0) == bool(want)
            if agrees:
                score += 10
                reasons.append("MACD histogram สอดคล้องทิศทาง → โมเมนตัมยืนยันฝั่งที่ต้องการ"
                               if want else
                               "MACD histogram เป็นลบ สอดคล้องทิศทาง → โมเมนตัมยืนยันฝั่งขาย")
            else:
                score -= 10
                reasons.append("MACD histogram สวนทางกับฝั่งที่ต้องการ → โมเมนตัมไม่ยืนยัน")

        # --- Volatility (0-20): moderate volatility is ideal ---
        if 0.4 <= ind.atr_pct <= 1.5:
            score += 15
            reasons.append(f"ATR {ind.atr_pct:.2f}% → ความผันผวนเหมาะสมต่อการเทรด")
        elif ind.atr_pct > 2.5:
            score -= 10
            reasons.append(f"ATR {ind.atr_pct:.2f}% → ผันผวนสูงเกิน ปรับลดขนาดโพซิชัน")
        else:
            score += 5

        # --- News & sentiment (0-20) ---
        # Sentiment is graded for the WANTED side: bullish news helps a BUY,
        # but the mirror-image bearish news now helps a SELL (old code applied
        # the raw sentiment with no direction, so bearish news always hurt).
        signed_sentiment = ind.news_sentiment * (1.0 if want else -1.0)
        score += max(-10.0, min(10.0, signed_sentiment * 10))
        if ind.high_impact_event:
            score -= 5
            reasons.append("มีข่าว impact สูงใกล้ตัว → ความเสี่ยง spike, รอให้ตลาดนิ่งก่อน")
        else:
            score += 5
            reasons.append("ไม่มีข่าว impact สูงระยะสั้น → สภาพแวดล้อมคาดการณ์ได้")

        # Supertrend + MACD agreement, now symmetric (SELL confirm too).
        if ind.supertrend_dir != 0 and ind.macd_hist != 0:
            agrees = ((ind.supertrend_dir > 0) == bool(want)
                      and (ind.macd_hist > 0) == bool(want))
            if agrees:
                score += 5
                reasons.append("Supertrend + MACD ยืนยันทิศเดียวกัน")

        # --- Breakout-retest bonus (strategy D — gold live snapshots) ------
        # Only a breakout in the WANTED direction counts: a downside break is
        # not a BUY bonus (old code always read breakout_state as an up-break).
        if ind.breakout_state == 2 and want:
            score += 5
            reasons.append(
                f"Breakout เหนือแนวต้าน 20 แท่ง ({ind.breakout_level:g}) — "
                "โมเมนตัมทะลุแนวยืนยันแล้ว")
        elif ind.breakout_state == 1 and want:
            score += 3
            reasons.append(
                f"Retest แนว breakout ({ind.breakout_level:g}) สำเร็จ — "
                "จุดเข้ายืนยันแล้ว ลดความเสี่ยง false breakout")
        elif ind.breakout_state == 2 and not want:
            score += 5
            reasons.append(
                f"Breakdown ใต้แนวรับ 20 แท่ง ({ind.breakout_level:g}) — "
                "โมเมนตัมทะลุลงยืนยันแล้ว")
        elif ind.breakout_state == 1 and not want:
            score += 3
            reasons.append(
                f"Retest แนว breakdown ({ind.breakout_level:g}) สำเร็จ — "
                "จุดเข้าขายยืนยันแล้ว")
        elif ind.asset.upper() == "XAUUSD":
            reasons.append(
                "ทอง: ยังไม่มีจังหวะ breakout/retest — รอการทะลุแนวสูง 20 แท่ง "
                "(โหมด Gold Breakout Only)")

        score = max(0.0, min(100.0, score))
        # P1-3: attach the INDEPENDENT evidence-confidence (same wanted side)
        # so callers get both axes in one object — the two are never the same
        # number (confidence = agreement, score = weighted quality).
        conf, conf_reasons = self.evidence_confidence(
            score, ind, direction=("BUY" if want else "SELL"))
        return AssetOpportunity(
            asset=ind.asset, score=round(score, 1),
            band=self.band_of(score), reasons=reasons or ["ข้อมูลไม่เพียงพอ — คะแนนกลาง"],
            confidence=conf, confidence_reasons=conf_reasons,
        )

    @staticmethod
    def _resolve_direction(ind: IndicatorSnapshot,
                           direction: Optional[str]) -> bool:
        """True = the wanted side is BUY, False = SELL.

        An explicit ``direction`` wins. ``None`` falls back to the legacy
        auto-detect (EMA trend, else Supertrend sign) so existing callers keep
        working; when BOTH are unavailable the side defaults to SELL-neutral
        (``False``) — matching the old ``bull_trend`` fallback, which was False
        when ``supertrend_dir <= 0``.
        """
        want_str = str(direction or "").strip().upper()
        if want_str in ("BUY", "LONG"):
            return True
        if want_str in ("SELL", "SHORT"):
            return False
        if ind.ema_fast and ind.ema_slow:
            return ind.ema_fast > ind.ema_slow
        return ind.supertrend_dir > 0

    @staticmethod
    def band_of(score: float) -> OpportunityBand:
        for lo, hi, band in BANDS:
            if lo <= score < hi or (hi == 100.0 and score == 100.0):
                return band
        return OpportunityBand.low

    # ------------------------------------------------------------------
    def build_proposal(
        self,
        ind: IndicatorSnapshot,
        opp: AssetOpportunity,
        risk_per_trade_pct: float,
        regime_bullish: bool,
        atr_multiple_sl: Optional[float] = None,
        rr_target: Optional[float] = None,
        risk_profile: RiskProfile = RiskProfile.moderate,
        sl_min_pct: Optional[float] = None,
        sl_max_pct: Optional[float] = None,
        invalidation_level: float = 0.0,
    ) -> SignalProposal:
        """Turn the scored snapshot into an explainable BUY/SELL proposal with SL/TP.

        sl_min_pct / sl_max_pct (Settings → SL distance clamp): pin the stop
        inside a % of price band so every asset risks a similar distance —
        close-only FX feeds understate ATR (no intraday wicks) while OHLC
        feeds don't, which made SLs drift 0.62%→1.17% per pair. 0 = off.

        invalidation_level (strategy D — gold breakout): when > 0 the SL is
        placed below/above the broken level with a 0.5×ATR buffer — the
        breakout structure IS the stop, so this distance wins even when
        tighter than the plain ATR stop (floored at 0.5×ATR so a too-close
        level can't create a hair-trigger SL). The structural stop is
        EXEMPT from the clamp — clamping it would break the level anchor.

        None → canonical AppSettings defaults (sl_distance_mode tier /
        rr_target / sl clamp): single source of truth, no literals here.
        """
        from app.models.schemas import S as _S, SL_TIER_MULT as _MULT
        if atr_multiple_sl is None:
            atr_multiple_sl = float(_MULT.get("medium", 1.5))
        if rr_target is None:
            rr_target = float(_S("rr_target"))
        if sl_min_pct is None:
            sl_min_pct = float(_S("sl_distance_min_pct"))
        if sl_max_pct is None:
            sl_max_pct = float(_S("sl_distance_max_pct"))
        direction = "BUY" if regime_bullish else "SELL"
        sign = 1 if regime_bullish else -1
        sl_distance = max(ind.price * ind.atr_pct / 100.0 * atr_multiple_sl, ind.price * 0.001)
        clamped = False
        if invalidation_level <= 0:  # clamp only the plain ATR stop
            if sl_max_pct > 0 and sl_distance > ind.price * sl_max_pct / 100.0:
                sl_distance, clamped = ind.price * sl_max_pct / 100.0, True
            if sl_min_pct > 0 and sl_distance < ind.price * sl_min_pct / 100.0:
                sl_distance, clamped = ind.price * sl_min_pct / 100.0, True
        if invalidation_level > 0:
            atr_price = ind.price * ind.atr_pct / 100.0
            invalidation_sl = (invalidation_level - 0.5 * atr_price if regime_bullish
                               else invalidation_level + 0.5 * atr_price)
            sl_distance = max(abs(ind.price - invalidation_sl),
                              atr_price * 0.5, ind.price * 0.001)
        stop_loss = ind.price - sign * sl_distance
        take_profit = ind.price + sign * sl_distance * rr_target
        # P1-3: prefer the EVIDENCE-agreement confidence computed alongside the
        # opportunity (opp.confidence). Fall back to the shim only for legacy
        # callers that built an AssetOpportunity without it (confidence==0).
        if getattr(opp, "confidence", 0.0):
            confidence = float(opp.confidence)
            conf_reasons = list(getattr(opp, "confidence_reasons", []) or [])
        else:
            confidence, conf_reasons = self.evidence_confidence(
                opp.score, ind, direction=direction)

        reasons = list(ind.reasons) or list(opp.reasons)
        reasons.insert(0, f"Opportunity Score {opp.score:.0f}/100 ({opp.band.value})")
        # Confidence is a SEPARATE axis (P1-3) — surface it right under the
        # opportunity so a card never implies "confidence == opportunity".
        reasons.insert(1, f"Confidence (หลักฐานเห็นด้วย) {confidence:.0f}/100")
        if clamped:
            lo = f"{sl_min_pct:g}%" if sl_min_pct > 0 else "—"
            hi = f"{sl_max_pct:g}%" if sl_max_pct > 0 else "—"
            reasons.insert(2, (
                f"SL ปรับเป็น {sl_distance / ind.price * 100:.2f}% ของราคา "
                f"(แถบกำหนด {lo}–{hi}) — ให้ทุกสัญลักษณ์เสี่ยงระยะใกล้เคียงกัน"))

        decision = self._decision(opp.score, ind)

        # --- Explainability: step-by-step Thai calc notes for the UI ---
        # The card renders these in a collapsible "วิธีคำนวณ" block so no
        # number appears without its derivation (SL from ATR, TP from RR,
        # ladder weights, tier multiples).
        calc_notes: list[str] = []
        try:
            sl_pct = sl_distance / ind.price * 100 if ind.price else 0.0
            tp_distance = abs(take_profit - ind.price)
            # P1-3 explainability: show HOW confidence was built (which
            # independent sources agreed) — a number without its evidence is
            # exactly the opacity P1-3 removes.
            if conf_reasons:
                calc_notes.append(
                    "Confidence = การเห็นด้วยของหลักฐานอิสระ (ไม่ใช่ค่าโอกาส): "
                    + " · ".join(conf_reasons))
            if invalidation_level > 0:
                calc_notes.append(
                    f"SL โครงสร้าง: อ้างอิงแนว breakout {invalidation_level:g} "
                    f"บวก buffer 0.5×ATR → SL ห่าง {sl_distance:g} "
                    f"({sl_pct:.2f}% ของราคา {ind.asset})")
            else:
                calc_notes.append(
                    f"SL = ATR {ind.atr_pct:.2f}% × {atr_multiple_sl:g} "
                    f"→ ห่าง {sl_distance:g} ({sl_pct:.2f}% ของราคา {ind.price:g})")
                if clamped:
                    lo = f"{sl_min_pct:g}%" if sl_min_pct > 0 else "—"
                    hi = f"{sl_max_pct:g}%" if sl_max_pct > 0 else "—"
                    calc_notes.append(
                        f"SL โดน clamp เข้าแถบ {lo}–{hi} → ใช้ {sl_pct:.2f}% "
                        f"แทนค่า ATR ดิบ (ให้ทุกคู่เสี่ยงระยะใกล้กัน)")
            calc_notes.append(
                f"TP = SL × RR 1:{rr_target:g} → ห่าง {tp_distance:g} "
                f"ที่ {take_profit:g} (ฝั่ง {direction})")
            calc_notes.append(
                f"ไม้ limit 3 ชั้นที่ −0.25/−0.50/−0.75×SL "
                f"น้ำหนัก 40/35/25% — SL/TP ทุกชั้นใช้ระยะ SL เดียวกัน")
            calc_notes.append(
                "Tier สั้น/กลาง/ยาว = ×1.0/×1.5/×2.0 ของ SL กลาง — "
                "แถวนี้เก็บราคากลาง ยิงจริงคำนวณใหม่ตามโหมดที่ตั้งไว้")
        except Exception:
            pass

        return SignalProposal(
            asset=ind.asset,
            direction=direction,
            confidence=confidence,
            entry=round(ind.price, 5),
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            expected_rr=rr_target,
            risk_per_trade_pct=risk_per_trade_pct,
            reason=reasons[:6],
            recommendation=decision,
            limit_levels=self.limit_ladder(
                direction, ind.price, sl_distance,
                rr_target=rr_target, atr_multiple_sl=atr_multiple_sl),
            sltp_levels=self.sltp_preview(
                direction, ind.price, sl_distance, rr_target=rr_target),
            calc_notes=calc_notes,
        )

    # ------------------------------------------------------------------
    # SL/TP distance tiers preview (สั้น / กลาง / ยาว)
    # ------------------------------------------------------------------
    @staticmethod
    def sltp_preview(direction: str, entry: float, sl_distance: float,
                     rr_target: Optional[float] = None) -> list[SLTPLevel]:
        """Preview SL/TP at 3 stop distances (สั้น ×1.0 / กลาง ×1.5 / ยาว ×2.0 ATR).

        The card always shows all 3 tiers; the user's sl_distance_mode setting
        decides which tier execute_signal actually uses for the real order.
        None → canonical AppSettings.rr_target.
        """
        if rr_target is None:
            from app.models.schemas import S as _S
            rr_target = float(_S("rr_target"))
        sign = 1 if direction == "BUY" else -1
        tiers = (("สั้น", 1.0), ("กลาง", 1.5), ("ยาว", 2.0))
        levels: list[SLTPLevel] = []
        for label, mult in tiers:
            dist = sl_distance * (mult / 1.5)  # sl_distance is the ×1.5 (กลาง) tier
            levels.append(SLTPLevel(
                label=label,
                atr_multiple=mult,
                stop_loss=round(entry - sign * dist, 5),
                take_profit=round(entry + sign * dist * rr_target, 5),
                rr=rr_target,
            ))
        return levels

    # ------------------------------------------------------------------
    @staticmethod
    def limit_ladder(
        direction: str,
        entry: float,
        sl_distance: float,
        rr_target: Optional[float] = None,
        atr_multiple_sl: Optional[float] = None,
    ) -> list[LimitLevel]:
        """Laddered limit entries (แนวรับหลายระดับ) spaced by fractions of the SL distance.

        BUY  → limits below market (buy the dip):  -0.25 / -0.50 / -0.75 × sl_distance
        SELL → mirrored above market:              +0.25 / +0.50 / +0.75 × sl_distance
        Risk split 40/35/25; each rung keeps the same SL distance and RR target.
        (sl_distance already includes the ATR multiple — same as the main SL.)
        None → canonical AppSettings.rr_target.
        """
        if rr_target is None:
            from app.models.schemas import S as _S
            rr_target = float(_S("rr_target"))
        sign = 1 if direction == "BUY" else -1
        steps = (0.25, 0.50, 0.75)
        weights = (40.0, 35.0, 25.0)
        levels: list[LimitLevel] = []
        for step, weight in zip(steps, weights):
            price = entry - sign * sl_distance * step
            level_sl = price - sign * sl_distance
            level_tp = price + sign * sl_distance * rr_target
            levels.append(LimitLevel(
                price=round(price, 5),
                risk_pct=weight,
                sl=round(level_sl, 5),
                tp=round(level_tp, 5),
                rr=rr_target,
            ))
        return levels

    # ------------------------------------------------------------------
    # P1-3: confidence = EVIDENCE AGREEMENT (independent of the score)
    # ------------------------------------------------------------------
    @staticmethod
    def evidence_confidence(score: float, ind: IndicatorSnapshot,
                            direction: Optional[str] = None) -> tuple[float, list[str]]:
        """Confidence 0-100 measuring how many INDEPENDENT signals AGREE with
        the wanted side — deliberately separate from ``opportunity_score``.

        Why this exists (P1-3): the old ``_confidence`` was just
        ``min(score, 90)`` — a monotone transform of the opportunity score.
        The scanner then wrote ``"confidence": opp.score`` into every row, so
        "opportunity" and "confidence" were the SAME NUMBER, and the signal
        gate compared the opportunity score against ``min_confidence``. The
        owner could not express "I want strong setups that are ALSO confirmed
        by many independent signals" — the two axes collapsed into one.

        This function scores EVIDENCE RELIABILITY instead:
          * a small base from the setup score (``min(score, 45)`` — a good
            setup is *some* evidence, but quality alone can never max out
            confidence);
          * + agreement for each independent source that CONFIRMS the wanted
            side (trend/EMA, ADX strength, Supertrend, MACD, RSI zone,
            sentiment, no-high-impact-news, breakout-retest);
          * − for each source that CONTRADICTS it.

        Direction-aware and symmetric (BUY-in-uptrend == SELL-in-downtrend),
        explainable (returns the per-source reasons), and fail-closed on a
        high-impact event (hard −30, mirroring the old −10 penalty's intent).

        Returns ``(confidence, reasons)``.
        """
        want = StrategyEngine._resolve_direction(ind, direction)
        reasons: list[str] = []

        # Weak quality anchor — quality alone caps at 45 so confidence can
        # only reach the high band through AGREEMENT.
        conf = min(max(float(score), 0.0), 45.0)
        reasons.append(f"ฐานจากคุณภาพเซ็ตอัป {conf:.0f}/45")

        agree, total = 0.0, 0

        # 1) Trend / EMA — the primary directional filter.
        if ind.ema_fast and ind.ema_slow:
            total += 1
            trend_up = ind.ema_fast > ind.ema_slow
            if trend_up == want:
                agree += 1
                reasons.append("EMA สอดคล้องทิศทาง (+1)")
            else:
                reasons.append("EMA สวนทางทิศทาง (−1)")
                agree -= 1
        # 2) ADX — independent trend-STRENGTH confirmation.
        if ind.adx > 0:
            total += 1
            if ind.adx >= 25:
                agree += 1
                reasons.append(f"ADX {ind.adx:.0f} ≥ 25 ยืนยันเทรนด์ (+1)")
            else:
                reasons.append(f"ADX {ind.adx:.0f} < 25 เทรนด์อ่อน (−1)")
                agree -= 1
        # 3) Supertrend sign — an independent trend gate.
        if ind.supertrend_dir != 0:
            total += 1
            if (ind.supertrend_dir > 0) == bool(want):
                agree += 1
                reasons.append("Supertrend สอดคล้องทิศทาง (+1)")
            else:
                agree -= 1
                reasons.append("Supertrend สวนทางทิศทาง (−1)")
        # 4) MACD histogram sign — momentum agreement.
        if ind.macd_hist != 0:
            total += 1
            if (ind.macd_hist > 0) == bool(want):
                agree += 1
                reasons.append("MACD สอดคล้องทิศทาง (+1)")
            else:
                agree -= 1
                reasons.append("MACD สวนทางทิศทาง (−1)")
        # 5) RSI zone — 40-70 (BUY) / 30-60 (SELL) supports, extremes warn.
        if ind.rsi:
            total += 1
            if want:
                good = 40 <= ind.rsi <= 70
            else:
                good = 30 <= ind.rsi <= 60
            if good:
                agree += 1
                reasons.append(f"RSI {ind.rsi:.0f} อยู่ในโซนที่สนับสนุน (+1)")
            else:
                reasons.append(f"RSI {ind.rsi:.0f} อยู่นอกโซนที่สนับสนุน (−1)")
                agree -= 1
        # 6) Sentiment aligned with the wanted side (not the raw sign).
        if ind.news_sentiment:
            total += 1
            signed = ind.news_sentiment * (1.0 if want else -1.0)
            if signed > 0:
                agree += 1
                reasons.append("Sentiment สอดคล้องทิศทาง (+1)")
            elif signed < 0:
                agree -= 1
                reasons.append("Sentiment สวนทางทิศทาง (−1)")

        # Agreement ratio → 0-55 points (the remaining headroom above the 45
        # quality anchor). With no evidence at all, the anchor stands alone.
        if total:
            ratio = max(0.0, min(1.0, (agree + total) / (2.0 * total)))
            conf += ratio * 55.0
            n_agree = int(round((agree + total) / 2.0))
            reasons.append(
                f"หลักฐานเห็นด้วย {n_agree}/{total} แหล่ง → +{ratio * 55.0:.0f}")

        # 7) Breakout-retest — a STRONG independent confirmation (only in the
        #    wanted direction; a downside break is not a BUY bonus).
        if ind.breakout_state == 2 and want:
            conf += 10
            reasons.append("Breakout ยืนยันทิศทาง (+10)")
        elif ind.breakout_state == 1 and want:
            conf += 5
            reasons.append("Retest ยืนยันทิศทาง (+5)")
        elif ind.breakout_state == 2 and not want:
            conf += 10
            reasons.append("Breakdown ยืนยันทิศทาง (+10)")
        elif ind.breakout_state == 1 and not want:
            conf += 5
            reasons.append("Retest breakdown ยืนยันทิศทาง (+5)")

        # High-impact news = hard reliability hit (do NOT double-count as a
        # risk gate — the execution news gate is the hard block; this only
        # lowers confidence so the two thresholds agree the setup is risky).
        if ind.high_impact_event:
            conf -= 30
            reasons.append("มีข่าว impact สูง → ความน่าเชื่อถือลดลง (−30)")

        return round(max(10.0, min(100.0, conf)), 1), reasons

    @staticmethod
    def _confidence(score: float, ind: IndicatorSnapshot) -> float:
        """Legacy 2-arg shim → evidence_confidence (P1-3).

        Kept so existing callers/tests (e.g. test_confidence_reduced_by_event)
        keep working; delegates to the evidence-agreement model, ignoring the
        returned reasons.
        """
        conf, _ = StrategyEngine.evidence_confidence(score, ind)
        return conf

    @staticmethod
    def _decision(opp_score: float, ind: IndicatorSnapshot) -> FinalDecision:
        if ind.high_impact_event:
            return FinalDecision.wait
        if opp_score >= 70:
            return FinalDecision.trade
        if opp_score >= 50:
            return FinalDecision.wait
        return FinalDecision.reduce_risk
