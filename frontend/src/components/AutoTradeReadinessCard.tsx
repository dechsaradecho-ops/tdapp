"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  AppSettings,
  CorrelationResponse,
  MonitorSnapshot,
  NewsRisk,
} from "@/lib/types";

/**
 * การ์ด "สถานะการเทรดอัตโนมัติ" บนหน้าหลัก — ตอบคำถามเดียว:
 * "ตอนนี้ระบบ auto เทรดเปิดไม้ได้หรือไม่ ได้/ไม่ได้เพราะปัจจัยอะไร"
 *
 * ปัจจัยทั้งหมด mirror gate pipeline จริงของ backend
 * (backend/app/services/execution.py :: _gate_blocked —
 * pause → kill switch → frequency → news → correlation → risk officer)
 * จึงสอดคล้องกับสิ่งที่ auto_trader ใช้ตัดสินใจจริงทุก 1 นาที
 */

type FactorState = "pass" | "fail" | "warn";

interface Factor {
  state: FactorState;
  label: string;
  detail: string;
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
      const [mon, settings, news, corr] = await Promise.all([
        api.monitor(),
        api.getSettings(),
        api.tradingCalendar(),
        api.tradingCorrelation().catch(() => null), // correlation พัง = ข้าม ไม่บล็อกการ์ด
      ]);
      setFactors(buildFactors(mon, settings, news, corr));
      const goldMin =
        settings.min_confidence_gold ?? settings.min_confidence;
      setSignalNote(
        `เงื่อนไขฝั่งสัญญาณ: confidence ≥ ${settings.min_confidence} ` +
          `(XAUUSD ≥ ${goldMin}) · opportunity ≥ ${settings.min_opportunity} · ` +
          `สินทรัพย์นั้นต้องไม่มีไม้เปิดค้าง · สัญญาณอายุ ≤ 30 นาที`,
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

  return (
    <section className="panel">
      <div className="flex items-center justify-between gap-3 mb-3">
        <h2 className="panel-title">🤖 สถานะการเทรดอัตโนมัติ</h2>
        <div className="flex items-center gap-2">
          {checkedAt && (
            <span className="text-xs text-slate-500">ตรวจเมื่อ {checkedAt}</span>
          )}
          <button
            onClick={load}
            disabled={loading}
            className="px-3 py-2 min-h-[36px] rounded-xl text-sm border border-white/15 bg-white/[0.04] text-slate-300 active:bg-white/10 disabled:opacity-50"
          >
            {loading ? "กำลังตรวจ..." : "ตรวจสอบ"}
          </button>
        </div>
      </div>

      {err ? (
        <p className="text-loss text-sm">{err}</p>
      ) : loading && factors.length === 0 ? (
        <p className="text-slate-500 text-sm">กำลังประเมินปัจจัยทั้งหมด...</p>
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
                <div className="min-w-0">
                  <p className={`text-sm font-semibold ${COLOR[f.state]}`}>
                    {f.label}
                  </p>
                  <p className="text-xs text-slate-400 break-words">{f.detail}</p>
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
  };
}

function buildFactors(
  mon: MonitorSnapshot,
  s: AppSettings,
  news: NewsRisk,
  corr: CorrelationResponse | null,
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
    });
  } else {
    f.push({
      state: "fail",
      label: "Correlation",
      detail: `พอร์ต ${corr.portfolio_correlation.toFixed(0)} > cap ${s.correlation_cap} — เสี่ยงซ้ำทิศเดียวกัน`,
    });
  }

  return f;
}
