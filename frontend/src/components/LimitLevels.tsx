"use client";

import { LimitLevel, SignalProposal } from "@/lib/types";
import { fmtNum } from "@/lib/format";

/**
 * Laddered entry cards — buy limit / sell limit ระดับ (แนวรับหลายแนว)
 * Each card: limit price, weight (%), per-level SL / TP at the target RR.
 */
export default function LimitLevels({ signal }: { signal: SignalProposal }) {
  if (!signal.limit_levels?.length) return null;
  const buy = signal.direction === "BUY";
  const label = buy ? "Buy Limit" : "Sell Limit";
  // 3 ระดับแนวรับ (rung 1-3) — ใช้ "ระดับ" เพื่อไม่ซ้ำกับ tier ระยะ SL/TP
  // (สั้น/กลาง/ยาว) ที่แสดงแยกใน SltpLevels
  const tierLabels = ["ระดับ 1", "ระดับ 2", "ระดับ 3"];
  const tierBadge = (i: number) =>
    tierLabels[i] ?? `ระดับ ${i + 1}`;

  return (
    <div className="mt-2">
      <p className="text-xs text-slate-500 mb-1">
        SL/TP 3 ระดับ — {label} แนวรับกระจายน้ำหนัก {signal.limit_levels.map((l) => `${l.risk_pct}%`).join(" / ")} (RR 1:{signal.expected_rr})
      </p>
      <div className="grid grid-cols-3 gap-2">
        {signal.limit_levels.map((lv: LimitLevel, i: number) => (
          <div key={i} className={`rounded p-2 border ${buy ? "border-profit/40 bg-profit/5" : "border-loss/40 bg-loss/5"}`}>
            <div className="flex items-center justify-between mb-1">
              <span className={`text-xs font-bold ${buy ? "text-profit" : "text-loss"}`}>
                {tierBadge(i)}
              </span>
              <span className="text-xs text-slate-500">{label} {i + 1} · {lv.risk_pct}%</span>
            </div>
            <p className="font-semibold text-sm">{fmtNum(lv.price, 5)}</p>
            <div className="mt-1 space-y-0.5 text-xs">
              <p className="text-loss">SL {fmtNum(lv.sl, 5)}</p>
              <p className="text-profit">TP {fmtNum(lv.tp, 5)}</p>
              <p className="text-slate-500">RR 1:{lv.rr}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
