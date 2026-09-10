"""Signal generation + SEMI-AUTO approval endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.engine.strategy_engine import StrategyEngine
from app.integrations import quotes
from app.models.schemas import (FinalDecision, QuoteFeedStatus, SignalProposal,
                                contract_value_for, effective_min_lot,
                                effective_spread,
                                risk_to_lot_for)
from app.services import execution
from app.services import signal_log
from app.services.execution import (
    SIGNAL_TTL_MIN,
    expire_stale_pending_signals,
    now_iso,
)
from app.services.notification_service import NotificationService

from app.api.routes.settings import get_app_settings

router = APIRouter()


class ApprovalRequest(BaseModel):
    signal_id: str
    approve: bool


async def _live_prices(assets: list[str]) -> tuple[dict[str, float], QuoteFeedStatus | None]:
    """Spot prices + feed health in one probe (prices feed the cards' live_price)."""
    if not assets:
        return {}, None
    try:
        prices, failures = await quotes.fetch_spot_prices(assets)
    except Exception as exc:
        prices, failures = {}, {a: str(exc) for a in assets}
    status = QuoteFeedStatus(
        state="ok" if not failures else "error",
        source="exchangerate+yahoo",
        fetched_at=datetime.now(timezone.utc),
        failed_assets=sorted(failures),
        message="; ".join(failures[a] for a in sorted(failures))[:300],
    )
    return prices, status


@router.get("/latest", response_model=list[SignalProposal])
async def latest_signals(request: Request) -> list[SignalProposal]:
    """Build explainable proposals from the latest opportunity snapshot."""
    db = request.app.state.db
    proposals: list[SignalProposal] = []
    # One settings load per request — risk sizing must follow the user's
    # saved risk_per_trade_pct, not a hardcoded 0.5.
    s = get_app_settings(db)

    # Self-heal: pending signals older than 30 min leave the queue first —
    # otherwise the page pins yesterday's entry prices (e.g. GBPUSD stuck
    # at 1.26797 while the live rate is 1.35) in semi_auto/manual modes
    # where the auto-trader never runs its expiry pass.
    expire_stale_pending_signals(db)

    rows = db.select("signals", limit=20)
    # Only live candidates: pending (semi-auto queue) + approved (auto-fired).
    # 'expired'/'rejected' rows are history — showing them made the page look
    # stuck on yesterday's entries. Rows with no approval value (legacy) count
    # as pending.
    rows = [r for r in rows
            if (r.get("approval") or "pending") in ("pending", "approved")]
    # Feed health probe + live spot prices (shared by every card below) —
    # non-fatal. live_price lets each card show the CURRENT market price
    # next to its entry, so a stale entry is visible at a glance instead of
    # silently looking like a fresh quote.
    live_prices, feed = await _live_prices(
        sorted({str(r.get("asset") or "").upper() for r in rows}))
    # Pending signals past the TTL were already expired by the pass above;
    # approved rows always stay visible. Cards render NEWEST → OLDEST (the
    # user's requested order): approved cards first (newest approval first),
    # then pending cards newest-first — the newest setup is the first card.
    if rows:
        # NEWEST → OLDEST across the whole page (user request 2026-09-04):
        # approved cards first in approval order (newest first), then pending
        # cards newest-first — the newest setup is the first card.
        rows.sort(key=lambda r: (
            (r.get("approval") or "pending") == "approved",
            r.get("approved_at") or r.get("created_at") or "",
        ), reverse=True)
        # Pending = action queue — keep EVERY pending card even when approved
        # history fills the page budget (pending is bounded by the scanner's
        # per-asset dedup, ≤1 per asset). The old combined rows[:8] slice let
        # 8 approved cards push the newest pending signals off the page
        # entirely (prod 2026-09-08: three 11:16 UTC pending cards invisible
        # → user reported "signal ใหม่ไม่ gen" while the scanner kept emitting).
        approved_rows = [r for r in rows if r.get("approval") == "approved"]
        pending_rows = [r for r in rows
                        if (r.get("approval") or "pending") == "pending"]
        rows = approved_rows[:8] + pending_rows
        # Read-time limit note — the scanner keeps generating signals all day
        # even past the user's limits (limits gate ORDER EXECUTION, not signal
        # generation), so pending cards that cannot fire right now carry the
        # reason: "ไม่ได้เปิดออเดอร์เพราะถึง limit แล้ว".
        open_rows = db.select("paper_trades", filters={"status": "open"},
                              limit=100)
        open_count = len(open_rows)
        # Assets that already hold an open position — the auto-trader skips
        # pending signals for these (duplicate-position gate), so the card
        # must say so instead of promising "~1 นาที" forever.
        open_assets = {str(r.get("asset") or "").upper() for r in open_rows}
        # Portfolio heat base (shared by every pending card below): open
        # risk $ = Σ|entry−SL|×lots×contract — the same math Gate 6 blocks
        # on, so the card explains the block before it happens.
        heat_open_usd = 0.0
        try:
            for _t in open_rows:
                if _t.get("stop_loss") and _t.get("entry_price"):
                    heat_open_usd += abs(float(_t["entry_price"]) - float(_t["stop_loss"])) \
                        * float(_t.get("volume") or 0) \
                        * contract_value_for(str(_t.get("asset") or ""))
        except Exception:
            heat_open_usd = 0.0
        heat_cap = float(getattr(s, "capital", 0) or 0)
        heat_open_pct = (heat_open_usd / heat_cap * 100.0) if heat_cap > 0 else 0.0
        heat_limit = float(getattr(s, "kill_daily_loss_pct", 2.0) or 2.0)
        today = datetime.now(timezone.utc).date().isoformat()
        todays = db.select("paper_trades", limit=500)
        today_count = len([r for r in todays
                           if str(r.get("created_at", ""))[:10] == today
                           and r.get("status") != "rejected"])
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()[:10]
        week_count = len([r for r in todays
                          if str(r.get("created_at", "")) >= week_ago
                          and r.get("status") != "rejected"])
        for r in rows:
            entry = float(r["entry"] or 0)
            stop_loss = float(r["stop_loss"] or 0)
            take_profit = float(r["take_profit"] or 0)
            sl_distance = abs(entry - stop_loss)
            rr = float(r["expected_rr"] or 2.0)
            ladder = (
                StrategyEngine.limit_ladder(r["direction"].upper(), entry, sl_distance)
                if entry > 0 and sl_distance > 0 else []
            )
            # Explainability (read-time): same sizing math execute_signal
            # uses — lots from risk_to_lot_for + min_lot floor, spread cost,
            # RR — so the card's "วิธีคำนวณ" matches the real order.
            calc_notes: list[str] = []
            try:
                asset_u = str(r.get("asset") or "").upper()
                if entry > 0 and sl_distance > 0:
                    sl_pct = sl_distance / entry * 100
                    calc_notes.append(
                        f"SL ห่าง {sl_distance:g} ({sl_pct:.2f}% ของ entry "
                        f"{entry:g}) ฝั่ง {str(r.get('direction') or '').upper()}")
                    tp_dist = abs(take_profit - entry) if take_profit else 0
                    calc_notes.append(
                        f"TP ห่าง {tp_dist:g} → RR 1:{rr:g} "
                        f"(TP {take_profit:g})")
                    lots = risk_to_lot_for(
                        float(s.capital or 0), float(s.risk_per_trade_pct or 0),
                        sl_distance, asset_u)
                    floor = effective_min_lot(s, asset_u)
                    lots_used = max(lots, floor)
                    contract = contract_value_for(asset_u)
                    risk_usd = sl_distance * lots_used * contract
                    calc_notes.append(
                        f"ขนาดไม้: ทุน ${float(s.capital or 0):g} × "
                        f"{float(s.risk_per_trade_pct or 0):g}% = "
                        f"${float(s.capital or 0) * float(s.risk_per_trade_pct or 0) / 100:g} "
                        f"÷ (SL {sl_distance:g} × contract {contract:g}) "
                        f"→ {lots:g} lots (floor {floor:g} → ใช้ {lots_used:g})")
                    calc_notes.append(
                        f"ถ้าโดน SL เสีย ${risk_usd:,.2f} "
                        f"({risk_usd / float(s.capital or 1) * 100:.2f}% ของทุน)")
                    spread = effective_spread(s, asset_u)
                    if spread > 0:
                        cost = spread * lots_used * contract
                        calc_notes.append(
                            f"สเปรด {spread:g} → ต้นทุนเปิดไม้ "
                            f"${cost:,.2f} (fill ±สเปรด/2)")
                    if heat_cap > 0:
                        _h_usd = sl_distance * lots_used * contract
                        _h_pct = _h_usd / heat_cap * 100.0
                        calc_notes.append(
                            f"Heat พอร์ต ${heat_open_usd:,.2f} ({heat_open_pct:.2f}%) "
                            f"+ ไม้นี้ ${_h_usd:,.2f} ({_h_pct:.2f}%) → "
                            f"รวม {heat_open_pct + _h_pct:.2f}% "
                            f"เทียบงบ daily {heat_limit:g}%")
            except Exception:
                pass
            # Why this pending signal cannot become an order right now —
            # open-position-per-asset gate + limits (quality/regime throttles
            # are scanner-side).
            order_block = ""
            if (r.get("approval") or "pending") == "pending":
                asset = str(r.get("asset") or "").upper()
                try:
                    _hl = risk_to_lot_for(float(s.capital or 0), float(s.risk_per_trade_pct or 0), sl_distance, asset)
                    _hl = max(_hl, effective_min_lot(s, asset))
                    _new_pct = (sl_distance * _hl * contract_value_for(asset) / heat_cap * 100.0) if (heat_cap > 0 and sl_distance > 0) else 0.0
                except Exception:
                    _new_pct = 0.0
                if asset in open_assets:
                    order_block = (f"ไม่ได้เปิดออเดอร์ใหม่เพราะ {asset} "
                                   f"มีไม้เปิดอยู่แล้ว — รอปิดไม้เดิมก่อน")
                elif heat_cap > 0 and heat_open_pct + _new_pct > heat_limit:
                    order_block = (f"ไม่ได้เปิดออเดอร์นี้เพราะ heat เต็ม "
                                   f"(ไม้เปิด {heat_open_pct:.2f}% + ไม้นี้ ~{_new_pct:.2f}% "
                                   f"เกินงบ daily {heat_limit:g}%) — รอปิดไม้เดิมก่อน")
                elif open_count >= s.max_open_positions:
                    order_block = (f"ไม่ได้เปิดออเดอร์นี้เพราะถึง limit แล้ว "
                                   f"(open positions {open_count}/"
                                   f"{s.max_open_positions})")
                elif today_count >= s.max_trades_daily:
                    order_block = (f"ไม่ได้เปิดออเดอร์นี้เพราะถึง limit แล้ว "
                                   f"(วันนี้ {today_count}/"
                                   f"{s.max_trades_daily})")
                elif week_count >= s.max_trades_weekly:
                    order_block = (f"ไม่ได้เปิดออเดอร์นี้เพราะถึง limit แล้ว "
                                   f"(สัปดาห์นี้ {week_count}/"
                                   f"{s.max_trades_weekly})")
            proposals.append(SignalProposal(
                asset=r["asset"], direction=r["direction"].upper(),
                confidence=float(r["confidence"]), entry=entry,
                stop_loss=stop_loss, take_profit=float(r["take_profit"] or 0),
                expected_rr=float(r["expected_rr"] or 2.0),
                risk_per_trade_pct=s.risk_per_trade_pct,
                # explanation เก็บแบบ " | "-joined — แตกกลับเป็นรายข้อเพื่อให้
                # การ์ดจัดหมวด เทรนด์/โมเมนตัม/ผันผวน/ข่าว ได้ (เดิมห่อทั้งก้อน
                # เป็นข้อเดียว classify เลยเทลงหมวดเดียวหมด)
                reason=[p.strip() for p in str(r.get("explanation") or "").split(" | ") if p.strip()],
                recommendation=FinalDecision.trade,
                limit_levels=ladder,
                sltp_levels=StrategyEngine.sltp_preview(
                    r["direction"].upper(), entry, sl_distance,
                    rr_target=float(r["expected_rr"] or 2.0))
                if entry > 0 and sl_distance > 0 else [],
                sl_distance_mode=s.sl_distance_mode,
                approval=r.get("approval") or "pending",
                approved_at=r.get("approved_at"),
                created_at=r.get("created_at"),
                order_blocked=order_block or None,
                live_price=live_prices.get(str(r["asset"]).upper()),
                feed_status=feed,
                calc_notes=calc_notes,
            ))
            # Countdown for pending cards: how long until this signal ages out
            # of the queue (30-min TTL) and the scanner re-evaluates the setup.
            if (r.get("approval") or "pending") == "pending" and r.get("created_at"):
                dt = execution._parse_dt(str(r["created_at"]))
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    left = SIGNAL_TTL_MIN - (
                        datetime.now(timezone.utc) - dt).total_seconds() / 60
                    proposals[-1].expires_min_left = round(max(left, 0.0), 1)
        return proposals

    # No stored signals → empty queue. No live/demo fallback: on-the-fly
    # cards built from live quotes or DEMO constants never passed the
    # scanner gates (min confidence / dedup / breakout) and look like real
    # tradeable signals — the UI shows "ยังไม่มีสัญญาณ — รอ Market Scanner".
    return proposals


@router.post("/approve")
async def approve_signal(payload: ApprovalRequest, request: Request):
    """SEMI-AUTO flow: user approves/rejects → execution gate → broker.

    The approved order goes through the SAME gate pipeline as the auto trader
    (pause → kill switch → frequency → news → correlation → risk officer) and
    is sized by risk_to_lot from settings — never the old hardcoded 0.01.
    """
    db = request.app.state.db
    status = "approved" if payload.approve else "rejected"
    db.update("signals", payload.signal_id, {"approval": status})

    if not payload.approve:
        # Lifecycle log: user said NO — pull the row so the log shows what
        # setup was rejected (asset/price), not just an opaque id.
        row = None
        try:
            rows = db.select("signals", filters={"id": payload.signal_id}, limit=1)
            row = rows[0] if rows else None
        except Exception:
            row = None
        signal_log.log_event(
            db=db, event="rejected", signal_id=str(payload.signal_id),
            asset=str((row or {}).get("asset") or ""),
            direction=str((row or {}).get("direction") or ""),
            confidence=(row or {}).get("confidence"),
            entry=(row or {}).get("entry"), source="user",
            reason="ผู้ใช้กดไม่อนุมัติสัญญาณ")
        return {"status": status}

    broker = request.app.state.broker
    signals = db.select("signals", filters={"id": payload.signal_id}, limit=1)
    if not signals:
        return {"status": status, "executed": False,
                "message": "signal row not found"}

    s = signals[0]
    srow = execution.get_app_settings(db)
    notifier = NotificationService(db, request.app.state.line)
    report = await execution.execute_signal(
        db, broker, notifier, srow,
        user_id=s.get("user_id", execution.DEFAULT_USER),
        asset=s["asset"], direction=s["direction"].upper(),
        entry=float(s["entry"] or 0), stop_loss=s.get("stop_loss"),
        take_profit=s.get("take_profit"),
        confidence=float(s.get("confidence") or 0),
        opportunity=float(s.get("opportunity_score") or s.get("confidence") or 0),
        signal_id=payload.signal_id, source="approved",
    )
    if not report.allowed:
        db.update("signals", payload.signal_id, {"approval": "rejected"})
        return {"status": "blocked", "executed": False,
                "rejects": report.rejects, "checks": report.checks}
    # Approval stamp — shown on the signals page (010 migration). The stamp
    # is a separate update on purpose: until 010 is applied the second call
    # is a no-op instead of failing the whole approval write.
    db.update("signals", payload.signal_id, {"approval": "approved"})
    db.update("signals", payload.signal_id, {"approved_at": now_iso()})
    return {"status": "executed", "executed": True,
            "volume": report.size_lots, "checks": report.checks}
