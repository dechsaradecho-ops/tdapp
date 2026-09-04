"use client";

import { SLTPLevel, SignalProposal } from "@/lib/types";
import { fmtNum } from "@/lib/format";

/**
 * SL/TP distance tiers — ระยะ SL/TP 3 ระดับ (สั้น ×1.0 / กลาง ×1.5 / ยาว ×2.0 ATR)
 * คำนวณจาก entry เดียวกัน (ไม่ใช่ ladder แนวรับ) — highlight ระดับที่ตรงกับ
 * sl_distance_mode ใน Settings เพราะนั่นคือระดับที่ระบบจะใช้เปิดออเดอร์จริง
 */
export default function SltpLevels({ signal }: { signal: SignalProposal }) {
  if (!signal.sltp_levels?.length) return null;
  // แปลง mode → atr_multiple เพื่อหา tier ที่จะ highlight
  const modeMultiple =
    signal.sl_distance_mode === "short" ? 1.0 : signal.sl_distance_mode === "long" ? 2.0 : 1.5;

  return (
    <div className="mt-2">
      <p className="text-xs text-slate-500 mb-1">
        ระยะ SL/TP จาก Entry — เลือกระดับใน Settings (highlight = ระดับที่ระบบจะเปิดออเดอร์)
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
                  : "border-slate-700 bg-surface"
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className={`text-xs font-bold ${active ? "text-accent" : "text-slate-300"}`}>
                  {lv.label}
                </span>
                <span className="text-[10px] text-slate-500">×{lv.atr_multiple} ATR</span>
              </div>
              <div className="space-y-0.5 text-xs">
                <p className="text-loss">SL {fmtNum(lv.stop_loss, 5)}</p>
                <p className="text-profit">TP {fmtNum(lv.take_profit, 5)}</p>
                <p className="text-slate-500">RR 1:{lv.rr}</p>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
