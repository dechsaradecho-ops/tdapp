"use client";

import Icon from "@/components/Icon";
import { fmtNum } from "@/lib/format";
import { MonitorOpenPosition } from "@/lib/types";

/**
 * Popup ยืนยันก่อนปิดไม้รายตัว (หน้า monitor ปุ่ม "ปิด") —
 * โชว์สรุปไม้ที่จะปิด + PnL ยังไม่ปิด ให้ยืนยันก่อนยิง
 * (ก่อนหน้านี้ปุ่มยิง api.closePosition ทันทีโดยไม่ถาม)
 */
export default function CloseSingleModal({
  position,
  busy,
  onConfirm,
  onClose,
}: {
  position: MonitorOpenPosition | null;
  busy: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  if (!position) return null;
  const win = position.unrealized_pnl >= 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade"
      style={{ background: "rgba(0,0,0,0.7)", WebkitBackdropFilter: "blur(16px) saturate(140%)", backdropFilter: "blur(16px) saturate(140%)" }}
      onClick={busy ? undefined : onClose}
    >
      <div
        className="panel w-full max-w-md space-y-4 animate-pop"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ---------- header ---------- */}
        <div className="flex items-center justify-between">
          <h3 className="panel-title flex items-center gap-1.5">
            <Icon n="warning" size={16} className="text-amber-400" /> ปิดไม้ — ยืนยันก่อนยิง
          </h3>
          <button
            onClick={onClose}
            disabled={busy}
            className="text-slate-400 hover:text-accent text-lg leading-none disabled:opacity-40"
            aria-label="ปิดหน้าต่าง"
          >
            ✕
          </button>
        </div>

        {/* ---------- สรุปไม้ที่จะปิด ---------- */}
        <div className={`rounded-lg p-4 text-center ${win ? "bg-profit/10" : "bg-loss/10"}`}>
          <p className="text-xs text-slate-400">
            {position.asset} · {position.direction === "BUY" ? "▲ BUY" : "▼ SELL"} · {fmtNum(position.volume, 2)} lots
          </p>
          <p className={`text-3xl font-bold ${win ? "text-profit" : "text-loss"}`}>
            {win ? "+" : ""}${fmtNum(position.unrealized_pnl, 2)}
          </p>
          <p className="text-xs text-slate-500 mt-1">PnL (ยังไม่ปิด) — ปิดที่ราคาปัจจุบัน · ทำแล้วย้อนกลับไม่ได้</p>
        </div>

        {/* ---------- รายละเอียด ---------- */}
        <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <div>
            <p className="text-xs text-slate-500">Entry</p>
            <p className="font-bold">{fmtNum(position.entry_price, 5)}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">ราคาปัจจุบัน</p>
            <p className="font-bold">{fmtNum(position.current_price, 5)}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">Ticket</p>
            <p className="font-mono text-xs break-all">{position.ticket || "-"}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">SL / TP</p>
            <p className="font-bold text-xs">
              {position.stop_loss != null ? fmtNum(position.stop_loss, 5) : "-"} / {position.take_profit != null ? fmtNum(position.take_profit, 5) : "-"}
            </p>
          </div>
        </div>

        {/* ---------- actions ---------- */}
        <div className="grid grid-cols-2 gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="border border-slate-700 rounded px-4 py-2.5 text-slate-300 font-semibold hover:bg-white/5 disabled:opacity-50 min-h-[44px]"
          >
            ยกเลิก
          </button>
          <button
            onClick={onConfirm}
            disabled={busy || !position.ticket}
            className="bg-loss text-white font-semibold rounded px-4 py-2.5 hover:opacity-90 disabled:opacity-50 min-h-[44px]"
          >
            {busy ? "กำลังปิด..." : "ยืนยันปิดไม้"}
          </button>
        </div>
      </div>
    </div>
  );
}
