"use client";

import Icon from "@/components/Icon";
import { fmtNum } from "@/lib/format";
import { MonitorOpenPosition } from "@/lib/types";

export type CloseGroupMode = "all" | "profit" | "loss";

const MODE_META: Record<CloseGroupMode, { title: string; api: string }> = {
  all: { title: "ปิดทั้งหมด", api: "positions/close-all" },
  profit: { title: "ปิดกำไร", api: "positions/close-group" },
  loss: { title: "ปิดขาดทุน", api: "positions/close-group" },
};

/**
 * Popup สรุปก่อนปิดไม้เป็นกลุ่ม (ปิดทั้งหมด / ปิดกำไร / ปิดขาดทุน) —
 * โชว์รายการไม้ที่จะถูกปิด พร้อม PnL รวมของกลุ่ม ให้ยืนยันก่อนยิง
 * (แทน window.confirm เดิม — ผู้ใช้ขอ popup สรุปก่อน 2026-09-07)
 */
export default function CloseGroupModal({
  mode,
  positions,
  busy,
  errorMsg,
  onConfirm,
  onClose,
}: {
  mode: CloseGroupMode | null;
  positions: MonitorOpenPosition[];
  busy: boolean;
  errorMsg: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  if (!mode) return null;
  const meta = MODE_META[mode];
  // เลือกไม้ที่จะถูกปิดตามโหมด — คำนวณจาก unrealized_pnl ที่หน้าจอเห็นอยู่
  const targets = mode === "profit"
    ? positions.filter((p) => p.unrealized_pnl > 0)
    : mode === "loss"
      ? positions.filter((p) => p.unrealized_pnl < 0)
      : positions;
  const groupPnl = targets.reduce((s, p) => s + p.unrealized_pnl, 0);
  const win = groupPnl >= 0;
  // ไม่มีไม้ในกลุ่ม (เช่น กดปิดกำไรตอนทุกไม้ยังขาดทุน) — บล็อกปุ่มยืนยัน
  const empty = targets.length === 0;

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
            <Icon n="warning" size={16} className="text-amber-400" /> {meta.title} — ยืนยันก่อนยิง
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

        {/* ---------- headline กลุ่ม ---------- */}
        <div className={`rounded-lg p-4 text-center ${empty ? "bg-slate-800/40" : win ? "bg-profit/10" : "bg-loss/10"}`}>
          <p className="text-xs text-slate-400">จะปิด {targets.length} ไม้ — PnL (ยังไม่ปิด) รวมของกลุ่ม</p>
          <p className={`text-3xl font-bold ${empty ? "text-slate-500" : win ? "text-profit" : "text-loss"}`}>
            {empty ? "—" : `${win ? "+" : ""}$${fmtNum(groupPnl, 2)}`}
          </p>
          <p className="text-xs text-slate-500 mt-1">ปิดที่ราคาปัจจุบัน (mark price) · ทำแล้วย้อนกลับไม่ได้</p>
        </div>

        {/* ---------- รายการไม้ที่จะถูกปิด ---------- */}
        {targets.length > 0 && (
          <div className="max-h-56 overflow-y-auto rounded-xl border border-white/10 divide-y divide-white/5">
            {targets.map((p) => (
              <div key={p.id} className="flex items-center justify-between px-3 py-2 text-sm">
                <span className="flex items-center gap-2 min-w-0">
                  <span className="font-semibold">{p.asset}</span>
                  <span className={`text-xs ${p.direction === "BUY" ? "text-profit" : "text-loss"}`}>
                    {p.direction === "BUY" ? "▲" : "▼"} {fmtNum(p.volume, 2)}
                  </span>
                  <span className="text-xs text-slate-500 truncate">{p.ticket}</span>
                </span>
                <span className={`font-bold ${p.unrealized_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                  {p.unrealized_pnl >= 0 ? "+" : ""}${fmtNum(p.unrealized_pnl, 2)}
                </span>
              </div>
            ))}
          </div>
        )}
        {empty && (
          <p className="text-sm text-slate-400 text-center">
            ไม่มีไม้ในกลุ่มนี้ (ตามเงื่อนไข {meta.title})
          </p>
        )}

        {errorMsg && (
          <p className="text-xs text-amber-400 flex items-start gap-1.5">
            <Icon n="warning" size={14} className="mt-0.5" />
            <span>{errorMsg}</span>
          </p>
        )}

        {/* ---------- actions ---------- */}
        <div className="grid grid-cols-2 gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="border border-slate-700 rounded px-4 py-2.5 text-slate-300 font-semibold hover:bg-white/5 disabled:opacity-50"
          >
            ยกเลิก
          </button>
          <button
            onClick={onConfirm}
            disabled={busy || empty}
            aria-busy={busy}
            className="bg-loss text-white font-semibold rounded px-4 py-2.5 hover:opacity-90 disabled:opacity-50"
          >
            <span className="inline-flex items-center justify-center gap-1.5">
              {busy && <Icon n="spinner" size={15} className="animate-spin" />}
              {busy ? "กำลังปิด..." : `ยืนยันปิด${targets.length > 0 ? ` ${targets.length} ไม้` : ""}`}
            </span>
          </button>
        </div>
      </div>
    </div>
  );
}
