"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import Icon from "@/components/Icon";
import LoadingGraphic from "@/components/LoadingGraphic";
import type {
  AppSettings,
  CorrelationResponse,
  GatePreview,
  MonitorSnapshot,
  NewsRisk,
} from "@/lib/types";

/**
 * การ์ด "สถานะการเทรดอัตโนมัติ" บนหน้าหลัก — ตอบคำถามเดียว:
 * "ตอนนี้ระบบ auto เทรดเปิดไม้ได้หรือไม่ ได้/ไม่ได้เพราะปัจจัยอะไร"
 *
 * ปัจจัยทั้งหมด mirror gate pipeline จริงของ backend
 * (backend/app/services/execution.py :: _gate_blocked —
 * pause → kill switch → frequency → cooldown (2b) → news →
 * pre-open: spread / pre-news / session (3b) → correlation →
 * currency exposure (4b) → risk officer)
 * จึงสอดคล้องกับสิ่งที่ auto_trader ใช้ตัดสินใจจริงทุก 1 นาที
 *
 * Gate 2b/3b/4b ไม่มี endpoint ไหนให้ข้อมูลได้ครบ จึงดึงจาก
 * GET /api/trading/gate-preview โดยเฉพาะ — และการ์ดต้องพูดให้ตรงกับ
 * semantics ของมัน: spread เป็น proxy จากไม้ที่เปิดอยู่ (ไม่เคยฟันธงว่าบล็อก),
 * currency เป็นค่าต่ำสุด (ไม้เปิดเท่านั้น) ส่วน session เป็นด่านระดับพอร์ตจริง
 */

type FactorState = "pass" | "fail" | "warn";

interface Factor {
  state: FactorState;
  label: string;
  detail: string;
  /** ถ้ามี = แสดง progress bar ใต้ detail (เช่น โควตา used/limit, correlation/cap) */
  progress?: { used: number; limit: number };
}

// ใช้สีแทนเครื่องหมายถูก/ผิด: เขียว = ผ่าน, แดง = ติด, เหลือง = เตือน
const DOT: Record<FactorState, string> = {
  pass: "bg-profit shadow-[0_0_8px_rgba(48,209,88,0.8)]",
  fail: "bg-loss shadow-[0_0_8px_rgba(255,69,58,0.8)]",
  warn: "bg-yellow-400 shadow-[0_0_8px_rgba(250,204,21,0.8)]",
};
const COLOR: Record<FactorState, string> = {
  pass: "text-profit",
  fail: "text-loss",
  warn: "text-yellow-400",
};
// สีแท่ง progress — เขียว = ยังเหลือเยอะ/ผ่าน, แดง = เต็มลิมิต/ติด, เหลือง = เตือน
const BAR: Record<FactorState, string> = {
  pass: "bg-profit",
  fail: "bg-loss",
  warn: "bg-yellow-400",
};

export default function AutoTradeReadinessCard() {
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [factors, setFactors] = useState<Factor[]>([]);
  const [signalNote, setSignalNote] = useState("");
  const [checkedAt, setCheckedAt] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const [mon, settings, news, corr, gp] = await Promise.all([
        api.monitor(),
        api.getSettings(),
        api.tradingCalendar(),
        api.tradingCorrelation().catch(() => null), // correlation พัง = ข้าม ไม่บล็อกการ์ด
        api.gatePreview().catch(() => null), // gate ใหม่พัง = ข้าม แต่การ์ดจะเตือนว่ายืนยันไม่ได้
      ]);
      setFactors(buildFactors(mon, settings, news, corr, gp));
      const goldMin =
        settings.min_confidence_gold ?? settings.min_confidence;
      setSignalNote(
        `เงื่อนไขฝั่งสัญญาณ: confidence ≥ ${settings.min_confidence} ` +
          `(XAUUSD ≥ ${goldMin}) · opportunity ≥ ${settings.min_opportunity} · ` +
          `สินทรัพย์นั้นต้องไม่มีไม้เปิดค้าง · สัญญาณอายุ ≤ 30 นาที · ` +
          `สเปรดต้องไม่กินระยะ SL · งดเปิดไม้ช่วงข่าวใหญ่/ตลาดปิด/สภาพคล่องต่ำ · ` +
          `ความเสี่ยงต่อสกุลเงินไม่เกินเพดาน`,
      );
      setCheckedAt(
        new Date().toLocaleTimeString("th-TH", {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }),
      );
    } catch {
      setErr("โหลดข้อมูลสถานะไม่สำเร็จ — ตรวจสอบว่า backend รันอยู่");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const blockers = factors.filter((f) => f.state === "fail");
  const canTrade = factors.length > 0 && blockers.length === 0;
  // แถบความพร้อมรวม: ปัจจัยที่ไม่ติด (pass + warn) / ทั้งหมด — warn ยังเทรดได้
  const okCount = factors.filter((f) => f.state !== "fail").length;
  const readinessPct = factors.length > 0
    ? Math.round((okCount / factors.length) * 100)
    : 0;

  return (
    <section className="panel">
      <div className="flex items-center justify-between gap-3 mb-3">
        <h2 className="panel-title flex items-center gap-1.5"><Icon n="bot" size={17} /> สถานะการเทรดอัตโนมัติ</h2>
        <div className="flex items-center gap-2">
          {checkedAt && (
            <span className="text-xs text-slate-500">ตรวจเมื่อ {checkedAt}</span>
          )}
          <button
            onClick={load}
            disabled={loading}
            aria-busy={loading}
            className="px-3 py-2 min-h-[36px] rounded-xl text-sm border border-white/15 bg-white/[0.04] text-slate-300 active:bg-white/10 disabled:opacity-50"
          >
            <span className="inline-flex items-center gap-1.5">
              {loading && <Icon n="spinner" size={14} className="animate-spin" />}
              {loading ? "กำลังตรวจ..." : "ตรวจสอบ"}
            </span>
          </button>
        </div>
      </div>

      {err ? (
        <p className="text-loss text-sm">{err}</p>
      ) : loading && factors.length === 0 ? (
        <LoadingGraphic message="กำลังประเมินปัจจัยทั้งหมด..." compact />
      ) : (
        <>
          {/* ---------- คำตัดสินรวม ---------- */}
          <div
            className={`rounded-xl border p-4 mb-3 ${
              canTrade ? "border-profit/40 bg-profit/10" : "border-loss/40 bg-loss/10"
            }`}
          >
            <p className={`text-lg font-bold ${canTrade ? "text-profit" : "text-loss"}`}>
              {canTrade
                ? "เปิดเทรดอัตโนมัติได้ตอนนี้"
                : "ยังเปิดเทรดอัตโนมัติไม่ได้ตอนนี้"}
            </p>
            <p className="text-sm text-slate-300 mt-1">
              {canTrade
                ? "ปัจจัยทุกข้อผ่านหมด — เมื่อสัญญาณใหม่เข้าเกณฑ์ ระบบจะยิงออเดอร์เองทันที (ทุก 1 นาที)"
                : `ติดปัจจัย ${blockers.length} ข้อ: ${blockers.map((b) => b.label).join(" · ")}`}
            </p>
            {/* แถบความพร้อมรวม — ผ่าน/ทั้งหมด */}
            <div className="mt-3">
              <div className="flex items-center justify-between text-xs text-slate-400 mb-1">
                <span>ความพร้อม {okCount}/{factors.length} ปัจจัย</span>
                <span className={`font-bold ${canTrade ? "text-profit" : "text-loss"}`}>{readinessPct}%</span>
              </div>
              <div
                className="h-2 w-full overflow-hidden rounded-full bg-white/10"
                role="progressbar"
                aria-valuenow={okCount}
                aria-valuemin={0}
                aria-valuemax={factors.length}
                aria-label={`ความพร้อม ${okCount} จาก ${factors.length} ปัจจัย`}
              >
                <div
                  className={`h-full rounded-full transition-all ${canTrade ? "bg-profit" : "bg-loss"}`}
                  style={{ width: `${readinessPct}%` }}
                />
              </div>
            </div>
          </div>

          {/* ---------- รายการปัจจัย ---------- */}
          <div className="grid sm:grid-cols-2 gap-2">
            {factors.map((f) => (
              <div
                key={f.label}
                className="rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2 flex items-start gap-2"
              >
                <span
                  className={`mt-1.5 w-2.5 h-2.5 rounded-full shrink-0 ${DOT[f.state]}`}
                />
                <div className="min-w-0 flex-1">
                  <p className={`text-sm font-semibold ${COLOR[f.state]}`}>
                    {f.label}
                  </p>
                  <p className="text-xs text-slate-400 break-words">{f.detail}</p>
                  {f.progress && (
                    <ProgressBar
                      used={f.progress.used}
                      limit={f.progress.limit}
                      state={f.state}
                    />
                  )}
                </div>
              </div>
            ))}
          </div>

          {signalNote && (
            <p className="text-xs text-slate-500 mt-3">{signalNote}</p>
          )}
        </>
      )}
    </section>
  );
}

function quota(label: string, used: number, limit: number, unit: string): Factor {
  const pass = used < limit;
  const left = Math.max(0, limit - used);
  return {
    state: pass ? "pass" : "fail",
    label,
    detail: pass
      ? `${used}/${limit} ${unit} — เหลืออีก ${left} ${unit}`
      : `${used}/${limit} ${unit} — เต็มลิมิตแล้ว รอรีเซ็ตหรือปิดไม้`,
    progress: { used, limit },
  };
}

/** แถบ progress ใต้ปัจจัยที่เป็นตัวเลข — กว้างตาม used/limit (ตันที่ 100%) */
function ProgressBar({ used, limit, state }: { used: number; limit: number; state: FactorState }) {
  const pct = limit > 0 ? Math.min(100, Math.max(0, (used / limit) * 100)) : 0;
  const label = `${used}/${limit}`;
  return (
    <div
      className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-white/10"
      role="progressbar"
      aria-valuenow={used}
      aria-valuemin={0}
      aria-valuemax={limit}
      aria-label={label}
      title={label}
    >
      <div
        className={`h-full rounded-full transition-all ${BAR[state]}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function buildFactors(
  mon: MonitorSnapshot,
  s: AppSettings,
  news: NewsRisk,
  corr: CorrelationResponse | null,
  gp: GatePreview | null,
): Factor[] {
  const f: Factor[] = [];

  // Gate หลัก: order_mode ต้องเป็น auto ไม่งั้น auto_trader ไม่ทำอะไรเลย
  if (mon.order_mode === "auto") {
    f.push({
      state: "pass",
      label: "โหมดเทรดอัตโนมัติ",
      detail: "order_mode = auto — ระบบยิงออเดอร์เองเมื่อมีสัญญาณ",
    });
  } else {
    f.push({
      state: "fail",
      label: "โหมดเทรดอัตโนมัติ",
      detail: `order_mode = ${mon.order_mode} — เปลี่ยนเป็น auto ที่หน้าตั้งค่า`,
    });
  }

  // Gate 0: manual pause switch
  if (!mon.pause.paused) {
    f.push({ state: "pass", label: "สวิตช์หยุดเทรด", detail: "ไม่ได้หยุดเทรดชั่วคราว" });
  } else {
    f.push({
      state: "fail",
      label: "สวิตช์หยุดเทรด",
      detail: `หยุดเทรดอยู่: ${mon.pause.reason || "manual pause"} — กด Resume บนมอนิเตอร์`,
    });
  }

  // Gate 1: kill switch
  if (!mon.kill.engaged) {
    f.push({
      state: "pass",
      label: "Kill Switch",
      detail: "ไม่มีทริกเกอร์ — ขาดทุน/ดรอว์ดาวน์อยู่ในลิมิต",
    });
  } else {
    f.push({
      state: "fail",
      label: "Kill Switch",
      detail: mon.kill.message || mon.kill.triggers[0] || "เกินลิมิตขาดทุน",
    });
  }

  // Gate 3: news block (DANGER = บล็อก, CAUTION = ผ่านแต่เตือน)
  if (news.status === "SAFE") {
    f.push({ state: "pass", label: "ข่าวเศรษฐกิจ", detail: "ไม่มีข่าวใหญ่ใกล้ปล่อยตัว" });
  } else if (news.status === "CAUTION") {
    f.push({
      state: "warn",
      label: "ข่าวเศรษฐกิจ",
      detail: `${news.reason || "มีข่าวใกล้ปล่อยตัว"} (ยังเทรดได้)`,
    });
  } else {
    f.push({
      state: "fail",
      label: "ข่าวเศรษฐกิจ",
      detail: news.reason || "ข่าว DANGER — ระงับเปิดไม้ชั่วคราว",
    });
  }

  // Gate 2: frequency quotas
  f.push(quota("โควตาเทรดวันนี้", mon.stats.trades_today, s.max_trades_daily, "ไม้"));
  f.push(quota("โควตาเทรด 7 วัน", mon.stats.trades_week, s.max_trades_weekly, "ไม้"));
  f.push(quota("ไม้เปิดค้าง", mon.stats.open_positions, s.max_open_positions, "ตำแหน่ง"));

  // Gate 4: correlation cap
  if (!corr) {
    f.push({
      state: "warn",
      label: "Correlation",
      detail: "โหลดไม่สำเร็จ — ไม่สามารถยืนยันได้",
    });
  } else if (corr.portfolio_correlation <= s.correlation_cap) {
    f.push({
      state: "pass",
      label: "Correlation",
      detail: `พอร์ต ${corr.portfolio_correlation.toFixed(0)}/cap ${s.correlation_cap} — ความสัมพันธ์สินทรัพย์ต่ำ`,
      progress: { used: corr.portfolio_correlation, limit: s.correlation_cap },
    });
  } else {
    f.push({
      state: "fail",
      label: "Correlation",
      detail: `พอร์ต ${corr.portfolio_correlation.toFixed(0)} > cap ${s.correlation_cap} — เสี่ยงซ้ำทิศเดียวกัน`,
      progress: { used: corr.portfolio_correlation, limit: s.correlation_cap },
    });
  }

  // ---------------------------------------------------------------------
  // Gate ใหม่ (migration 041) — ข้อมูลทั้งหมดมาจาก /api/trading/gate-preview
  // ---------------------------------------------------------------------
  // โหลดไม่ได้ = ยืนยันไม่ได้ → warn (ไม่ใช่ pass) เพื่อไม่ให้การ์ดบอกว่า
  // "พร้อมเทรด" ทั้งที่ยังไม่ได้ตรวจด่าน 3b/4b เลย
  if (!gp) {
    f.push({
      state: "warn",
      label: "Gate ก่อนเปิดไม้ (2b/3b/4b)",
      detail: "โหลด gate-preview ไม่สำเร็จ — ยืนยันสเปรด/ข่าว/ช่วงเวลา/สกุลเงินไม่ได้",
    });
    return f;
  }

  // Gate 2b: cooldown เปิดซ้ำสัญลักษณ์เดิม — รายสัญลักษณ์ ไม่ได้หยุดทั้งพอร์ต
  if (!gp.cooldown.enabled) {
    f.push({
      state: "warn",
      label: "Cooldown เปิดซ้ำ",
      detail: "ยังไม่ตั้ง cooldown — เปิดไม้สัญลักษณ์เดิมซ้ำได้ทันที",
    });
  } else if (gp.cooldown.active.length > 0) {
    const names = gp.cooldown.active.map((c) => c.asset).join(", ");
    f.push({
      state: "warn",
      label: "Cooldown เปิดซ้ำ",
      detail: `${gp.cooldown.active.length} คู่เพิ่งปิดภายใน ${gp.cooldown.minutes} นาที: ${names} — เปิดซ้ำคู่นี้ตอนนี้จะถูกเลื่อน`,
    });
  } else {
    f.push({
      state: "pass",
      label: "Cooldown เปิดซ้ำ",
      detail: `ไม่มีคู่ไหนเพิ่งปิดภายใน ${gp.cooldown.minutes} นาที`,
    });
  }

  // Gate 3b (1): spread guard — ⚠️ proxy จาก "ไม้ที่เปิดอยู่" (endpoint ตั้งใจ
  // ไม่ส่ง blocking ให้หัวข้อนี้) เพราะ gate จริงวัดสเปรดตอนไม้ใหม่เปิด
  if (!gp.spread.enabled) {
    f.push({
      state: "warn",
      label: "สเปรดก่อนเปิดไม้",
      detail: "ยังไม่ตั้งเพดานสเปรด — ปิดการตรวจสเปรดอยู่",
    });
  } else if (gp.spread.worst_asset === null || gp.spread.worst_pct === null) {
    f.push({
      state: "pass",
      label: "สเปรดก่อนเปิดไม้",
      detail: `ยังไม่มีไม้เปิดให้ประเมิน — เพดาน ${gp.spread.cap_pct}% ของระยะ SL`,
    });
  } else {
    const spreadOver = gp.spread.worst_pct > gp.spread.cap_pct;
    f.push({
      state: spreadOver ? "warn" : "pass",
      label: "สเปรดก่อนเปิดไม้",
      detail: spreadOver
        ? `แย่สุด ${gp.spread.worst_asset} ${gp.spread.worst_pct}%/เพดาน ${gp.spread.cap_pct}% ` +
          "(วัดจากไม้เปิดอยู่) — ไม้ใหม่ที่ SL แคบกว่านี้จะถูกบล็อก"
        : `แย่สุด ${gp.spread.worst_asset} ${gp.spread.worst_pct}%/เพดาน ${gp.spread.cap_pct}% ` +
          "(วัดจากไม้เปิดอยู่) — สเปรดกินระยะ SL น้อย",
      progress: { used: gp.spread.worst_pct, limit: gp.spread.cap_pct },
    });
  }

  // Gate 3b (2): งดเปิดไม้ก่อนข่าว high-impact ที่กระทบสกุลในพอร์ต
  if (!gp.pre_news.enabled) {
    f.push({
      state: "warn",
      label: "ข่าวก่อนเปิดไม้",
      detail: "ยังไม่ตั้งเวลางดเปิดไม้ก่อนข่าว — ปิดการตรวจนี้อยู่",
    });
  } else if (gp.pre_news.blocking) {
    f.push({
      state: "fail",
      label: "ข่าวก่อนเปิดไม้",
      detail:
        `ข่าว ${gp.pre_news.event ?? "high-impact"} (${gp.pre_news.currency ?? "-"}) ` +
        `อีก ${Math.round(gp.pre_news.minutes_to_next ?? 0)} นาที — งดเปิดไม้ใหม่ ` +
        `· กระทบ ${gp.pre_news.affected_assets.join(", ")}`,
    });
  } else if (gp.pre_news.in_window) {
    f.push({
      state: "warn",
      label: "ข่าวก่อนเปิดไม้",
      detail:
        `ข่าว ${gp.pre_news.event ?? "-"} (${gp.pre_news.currency ?? "-"}) ` +
        `อีก ${Math.round(gp.pre_news.minutes_to_next ?? 0)} นาที — ไม่กระทบสกุลในพอร์ต (ยังเทรดได้)`,
    });
  } else {
    f.push({
      state: "pass",
      label: "ข่าวก่อนเปิดไม้",
      detail: gp.pre_news.minutes_to_next !== null
        ? `ข่าวใหญ่ตัวถัดไปอีก ${Math.round(gp.pre_news.minutes_to_next)} นาที — ไกลจากหน้าต่างงดเปิดไม้ ${gp.pre_news.flatten_min} นาที`
        : `ไม่มีข่าว high-impact ในปฏิทิน — หน้าต่างงดเปิดไม้ ${gp.pre_news.flatten_min} นาที`,
    });
  }

  // Gate 0b (ตลาดปิด) + Gate 3b (3) session — ด่านระดับพอร์ตจริง blocking
  // จึงฟันธงได้. ตลาดปิดเป็นกฎเหล็ก (ไม่ใช่ setting) → ต้องขึ้น fail เสมอ
  // แม้ session filter จะปิดอยู่ (ไม่งั้นการ์ดจะโกหกว่ายิงออเดอร์ได้ทุกช่วงเวลา)
  if (gp.session.market_closed) {
    f.push({
      state: "fail",
      label: "ช่วงเวลาเทรด",
      detail:
        gp.session.market_block ||
        "ตลาดปิด (weekend) — ห้ามเปิดออเดอร์ใหม่ กัน gap วันจันทร์",
    });
  } else if (!gp.session.enabled) {
    f.push({
      state: "warn",
      label: "ช่วงเวลาเทรด",
      detail: "ยังไม่เปิด session filter — ระบบเปิดไม้ได้ทุกช่วงเวลา",
    });
  } else if (gp.session.blocking) {
    f.push({
      state: "fail",
      label: "ช่วงเวลาเทรด",
      detail: gp.session.reason || "งดเปิดไม้ใหม่ในช่วงนี้",
    });
  } else {
    f.push({
      state: "pass",
      label: "ช่วงเวลาเทรด",
      detail:
        `session: ${gp.session.active_sessions.join(", ") || "-"} · ` +
        `สภาพคล่อง ${gp.session.volatility_hint}` +
        (gp.session.overlapping ? " · ทับซ้อน (ดี)" : ""),
    });
  }

  // Gate 4b: ความเสี่ยงต่อสกุลเงิน ณ จุด SL — ค่าต่ำสุด (คิดจากไม้เปิดเท่านั้น)
  if (!gp.currency.enabled) {
    f.push({
      state: "warn",
      label: "ความเสี่ยงต่อสกุลเงิน",
      detail: "ยังไม่ตั้งเพดานความเสี่ยงต่อสกุลเงิน — ปิดการตรวจนี้อยู่",
    });
  } else {
    const c = gp.currency;
    const dir =
      c.direction === "long" ? "ยาว" : c.direction === "short" ? "สั้น" : (c.direction ?? "-");
    if (c.over_cap) {
      f.push({
        state: "fail",
        label: "ความเสี่ยงต่อสกุลเงิน",
        detail:
          `สกุล ${c.currency ?? "-"} เอียง${dir} ${c.pct.toFixed(1)}% เกินเพดาน ${c.cap_pct}% ` +
          `(เสี่ยง $${c.risk_usd.toFixed(2)}) — ไม้เปิดทับสกุลเดียวกัน รอปิดไม้เดิมก่อน`,
        progress: { used: c.pct, limit: c.cap_pct },
      });
    } else {
      f.push({
        state: "pass",
        label: "ความเสี่ยงต่อสกุลเงิน",
        detail:
          `แย่สุด ${c.currency ?? "-"} (${dir}) ${c.pct.toFixed(1)}% จากเพดาน ${c.cap_pct}% — ` +
          "คิดจากไม้เปิดเท่านั้น ไม้ใหม่จะบวกความเสี่ยงเพิ่ม",
        progress: { used: c.pct, limit: c.cap_pct },
      });
    }
  }

  return f;
}
