"use client";

import { useState } from "react";
import { SLTPLevel, SignalProposal } from "@/lib/types";
import CopyNum from "@/components/CopyNum";

/**
 * SL/TP 3 ระดับ (สั้น ×1.0 / กลาง ×1.5 / ยาว ×2.0 ATR) — บล็อกอ้างอิงแบบพับ
 * ค่าหลักที่ระบบจะยิงจริงคือ SL/TP ด้านบนของการ์ด (effective = tier ตาม
 * sl_distance_mode + SL cap) ไม่ใช่ค่าดิบใน 3 ช่องนี้ — 3 ช่องนี้ไว้เทียบ
 * ว่าระดับอื่นห่างแค่ไหนเท่านั้น (พับเป็นค่าเริ่มต้นเหมือน CalcNotes)
 */
export default function SltpLevels({ signal }: { signal: SignalProposal }) {
  const [open, setOpen] = useState(false);
  if (!signal.sltp_levels?.length) return null;
  // แปลง mode → atr_multiple เพื่อหา tier ที่ตรงกับค่า effective ด้านบน
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
          3 ระดับ (อ้างอิง) — ค่าหลักคือ SL/TP ด้านบน
        </span>
        <span className="text-slate-500 shrink-0 ml-2">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
      <div className="px-2 pb-2">
      <p className="text-xs text-slate-500 mb-1">
        เทียบระยะจาก Entry เดียวกัน (highlight = tier ตรงกับค่าหลักด้านบน)
      </p>
      <div className="grid grid-cols-3 gap-2">
        {signal.sltp_levels.map((lv: SLTPLevel) => {
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
  );
}
