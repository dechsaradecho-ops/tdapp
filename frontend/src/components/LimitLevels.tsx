"use client";

import { useState } from "react";
import { LimitLevel, SignalProposal } from "@/lib/types";
import CopyNum from "@/components/CopyNum";

/**
 * Laddered entry cards — buy limit / sell limit ระดับ (แนวรับหลายแนว)
 * Each card: limit price, weight (%), per-level SL / TP at the target RR.
 */
export default function LimitLevels({ signal }: { signal: SignalProposal }) {
  const levels = signal.limit_levels ?? [];
  const [open, setOpen] = useState(false);
  if (!levels.length) return null;
  const buy = signal.direction === "BUY";
  const label = buy ? "Buy Limit" : "Sell Limit";
  // 3 ระดับแนวรับ (rung 1-3) — แสดงแค่เลข 1/2/3 ใน pill (ผู้ใช้ขอ 2026-09-07:
  // "ระดับ 1,2,3 ให้เหลือแค่ 1.2.3") ไม่ซ้ำกับ tier ระยะ SL/TP (สั้น/กลาง/ยาว) ใน SltpLevels
  const tierLabels = ["1", "2", "3"];
  const tierBadge = (i: number) =>
    tierLabels[i] ?? String(i + 1);

  return (
    <div className="mt-2 rounded-xl border border-white/10 bg-white/[0.04]">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-2 py-1.5 text-xs hover:bg-white/10 rounded-xl"
        aria-expanded={open}
      >
        <span className="text-slate-400">
          SL/TP 3 ระดับ — {label} แนวรับกระจายน้ำหนัก {levels.map((l) => `${l.risk_pct}%`).join(" / ")} (RR 1:{signal.expected_rr})
        </span>
        <span className="text-slate-500 shrink-0 ml-2">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
      <div className="grid grid-cols-3 gap-2 px-2 pb-2">
        {levels.map((lv: LimitLevel, i: number) => (
          <div key={i} className={`rounded p-2 border ${buy ? "border-profit/40 bg-profit/5" : "border-loss/40 bg-loss/5"}`}>
            <div className="flex items-center justify-between mb-1">
              <span className={`inline-flex items-center justify-center min-w-[22px] h-[22px] rounded-full px-1.5 text-xs font-bold ${buy ? "bg-profit/30 text-profit" : "bg-loss/30 text-loss"}`}>
                {tierBadge(i)}
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
      )}
    </div>
  );
}
