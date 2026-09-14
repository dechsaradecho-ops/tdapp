"use client";

import { useState } from "react";
import { LimitLevel, SignalProposal, SLTPLevel } from "@/lib/types";
import CopyNum from "@/components/CopyNum";

/**
 * รวม 2 บล็อก 3 ระดับไว้ในพับเดียว (ผู้ใช้ขอ: พับเข้าด้วยกัน)
 * - ชุดบน: Limit ladder (Buy/Sell Limit แนวรับ 1/2/3 กระจายน้ำหนัก)
 * - ชุดล่าง: SL/TP tiers สั้น/กลาง/ยาว (อ้างอิง — ค่าหลักคือ SL/TP ด้านบนการ์ด)
 * ปุ่มเดียวคุมทั้งคู่ (open state เดียว) — กางทีเดียวเห็นครบ
 */
export default function SignalLevels({ signal }: { signal: SignalProposal }) {
  const [open, setOpen] = useState(false);
  const levels = signal.limit_levels ?? [];
  const tiers = signal.sltp_levels ?? [];
  if (!levels.length && !tiers.length) return null;
  const buy = signal.direction === "BUY";
  const label = buy ? "Buy Limit" : "Sell Limit";
  const tierLabels = ["1", "2", "3"];
  const modeMultiple =
    signal.sl_distance_mode === "short" ? 1.0 : signal.sl_distance_mode === "long" ? 2.0 : 1.5;

  return (
    <div className="mt-2 rounded-xl border border-white/10 bg-white/[0.04]">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-2 py-1.5 text-xs hover:bg-white/10 rounded-xl"
        aria-expanded={open}
      >
        <span className="text-slate-400">
          3 ระดับ (อ้างอิง) — {levels.length > 0 ? `${label} ${levels.map((l) => `${l.risk_pct}%`).join(" / ")} + ` : ""}SL/TP สั้น/กลาง/ยาว (RR 1:{signal.expected_rr})
        </span>
        <span className="text-slate-500 shrink-0 ml-2">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
      <div className="px-2 pb-2 space-y-3">
        {levels.length > 0 && (
        <div>
          <p className="text-xs text-slate-500 mb-1">
            {label} แนวรับกระจายน้ำหนัก
          </p>
          <div className="grid grid-cols-3 gap-2">
            {levels.map((lv: LimitLevel, i: number) => (
              <div key={i} className={`rounded p-2 border ${buy ? "border-profit/40 bg-profit/5" : "border-loss/40 bg-loss/5"}`}>
                <div className="flex items-center justify-between mb-1">
                  <span className={`inline-flex items-center justify-center min-w-[22px] h-[22px] rounded-full px-1.5 text-xs font-bold ${buy ? "bg-profit/30 text-profit" : "bg-loss/30 text-loss"}`}>
                    {tierLabels[i] ?? String(i + 1)}
                  </span>
                  <span className="text-xs text-slate-500">{label} {i + 1} · {lv.risk_pct}%</span>
                </div>
                <p className="font-bold text-sm"><CopyNum value={lv.price} /></p>
                <div className="mt-1 space-y-1 text-xs">
                  <p><span className="inline-flex items-center gap-1 rounded-full bg-loss/30 text-loss px-2 py-0.5 font-bold">SL <CopyNum value={lv.sl} /></span></p>
                  <p><span className="inline-flex items-center gap-1 rounded-full bg-profit/30 text-profit px-2 py-0.5 font-bold">TP <CopyNum value={lv.tp} /></span></p>
                  <p className="text-slate-500">RR 1:{lv.rr}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
        )}
        {tiers.length > 0 && (
        <div>
          <p className="text-xs text-slate-500 mb-1">
            เทียบระยะจาก Entry เดียวกัน (highlight = tier ตรงกับค่าหลักด้านบน)
          </p>
          <div className="grid grid-cols-3 gap-2">
            {tiers.map((lv: SLTPLevel) => {
              const active = lv.atr_multiple === modeMultiple;
              return (
                <div
                  key={lv.label}
                  className={`rounded p-2 border ${
                    active
                      ? "border-accent bg-accent/10"
                      : "border-white/10 bg-white/[0.04]"
                  }`}
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className={`text-xs font-bold ${active ? "text-accent" : "text-slate-300"}`}>
                      {lv.label}
                    </span>
                    <span className="text-[10px] text-slate-500">×{lv.atr_multiple} ATR</span>
                  </div>
                  <div className="space-y-1 text-xs">
                    <p><span className="inline-flex items-center gap-1 rounded-full bg-loss/30 text-loss px-2 py-0.5 font-bold">SL <CopyNum value={lv.stop_loss} /></span></p>
                    <p><span className="inline-flex items-center gap-1 rounded-full bg-profit/30 text-profit px-2 py-0.5 font-bold">TP <CopyNum value={lv.take_profit} /></span></p>
                    <p className="text-slate-500">RR 1:{lv.rr}</p>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
        )}
      </div>
      )}
    </div>
  );
}
