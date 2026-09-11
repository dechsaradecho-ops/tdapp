"use client";

import Icon from "@/components/Icon";
import { fmtNum } from "@/lib/format";
import { ExtendedOpenResult } from "@/lib/types";

export interface ExtendedPlanLeg {
  order_type?: string;
  price?: number;
  lot?: number;
  note?: string;
}

export interface ExtendedPlanView {
  asset?: string;
  direction?: string;
  average_entry?: number;
  stop_loss?: number;
  take_profit?: number;
  entries?: ExtendedPlanLeg[];
  rationale?: string[];
}

/**
 * Popup สรุปก่อนเปิดออเดอร์จาก ORDER STRATEGY (Extended) —
 * เปิดเฉพาะขา Market แรก, FINAL WAIT ไม่ให้เปิด (ปุ่ม disabled ตั้งแต่กล่อง).
 */
export function ExtendedOpenConfirmModal({
  plan,
  finalDecision,
  busy,
  errorMsg,
  onConfirm,
  onClose,
}: {
  plan: ExtendedPlanView | null;
  finalDecision: string;
  busy: boolean;
  errorMsg: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  if (!plan) return null;
  const first = plan.entries?.[0];
  const rest = Math.max((plan.entries?.length ?? 1) - 1, 0);
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade"
      style={{ background: "rgba(0,0,0,0.7)", WebkitBackdropFilter: "blur(16px) saturate(140%)", backdropFilter: "blur(16px) saturate(140%)" }}
      onClick={busy ? undefined : onClose}
    >
      <div className="panel w-full max-w-md space-y-4 animate-pop" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h3 className="panel-title flex items-center gap-1.5">
            <Icon n="warning" size={16} className="text-amber-400" /> เปิดออเดอร์ — ยืนยันก่อนยิง
          </h3>
          <button onClick={onClose} disabled={busy}
            className="text-slate-400 hover:text-accent text-lg leading-none disabled:opacity-40" aria-label="ปิดหน้าต่าง">✕</button>
        </div>

        <div className="rounded-lg p-4 text-center bg-accent/10">
          <p className="text-xs text-slate-400">{plan.asset} · {plan.direction === "BUY" ? "▲ BUY" : "▼ SELL"}</p>
          <p className="text-3xl font-bold">ขา Market แรก</p>
          <p className="text-xs text-slate-500 mt-1">
            เปิดเฉพาะขาแรกขาเดียว · อีก {rest} ขาที่เหลือในแผนไม่ถูกเปิด
          </p>
        </div>

        <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <div>
            <p className="text-xs text-slate-500">Entry (ขาแรก)</p>
            <p className="font-bold">{first?.price != null ? fmtNum(first.price, 5) : "-"}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">Lot (แผน)</p>
            <p className="font-bold">{first?.lot != null ? fmtNum(first.lot, 2) : "-"}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">SL</p>
            <p className="font-bold">{plan.stop_loss != null ? fmtNum(plan.stop_loss, 5) : "-"}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">TP</p>
            <p className="font-bold">{plan.take_profit != null ? fmtNum(plan.take_profit, 5) : "-"}</p>
          </div>
        </div>

        <p className="text-xs text-slate-500">
          FINAL: <span className="text-slate-300 font-semibold">{finalDecision}</span>
          {" · "}ยิงผ่าน gate เดียวกับปุ่มอนุมัติ (re-anchor ราคาจริง + ใช้ lot ตามแผนขาแรก + แจ้ง LINE + เก็บ log)
        </p>
        {errorMsg && <p className="text-xs text-loss">{errorMsg}</p>}

        <div className="grid grid-cols-2 gap-2">
          <button onClick={onClose} disabled={busy}
            className="border border-slate-700 rounded px-4 py-2.5 text-slate-300 font-semibold hover:bg-white/5 disabled:opacity-50 min-h-[44px]">
            ยกเลิก
          </button>
          <button onClick={onConfirm} disabled={busy} aria-busy={busy}
            className="bg-accent text-white font-semibold rounded px-4 py-2.5 hover:opacity-90 disabled:opacity-50 min-h-[44px]">
            <span className="inline-flex items-center justify-center gap-1.5">
              {busy && <Icon n="spinner" size={15} className="animate-spin" />}
              {busy ? "กำลังเปิด..." : "ยืนยันเปิดออเดอร์"}
            </span>
          </button>
        </div>
      </div>
    </div>
  );
}

/** Popup ผลหลังยิงเปิดออเดอร์ (สำเร็จ / ถูกบล็อกโดย gate). */
export function ExtendedOpenResultModal({
  result,
  onClose,
}: {
  result: ExtendedOpenResult | null;
  onClose: () => void;
}) {
  if (!result) return null;
  const ok = result.ok;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade"
      style={{ background: "rgba(0,0,0,0.7)", WebkitBackdropFilter: "blur(16px) saturate(140%)", backdropFilter: "blur(16px) saturate(140%)" }}
      onClick={onClose}
    >
      <div className="panel w-full max-w-md space-y-4 animate-pop" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h3 className="panel-title flex items-center gap-1.5">
            {ok
              ? <><Icon n="checkCircle" size={16} className="text-profit" /> เปิดออเดอร์สำเร็จ</>
              : <><Icon n="xCircle" size={16} className="text-loss" /> เปิดไม่สำเร็จ</>}
          </h3>
          <button onClick={onClose} className="text-slate-400 hover:text-accent text-lg leading-none" aria-label="ปิดหน้าต่าง">✕</button>
        </div>

        <p className="text-sm text-slate-300">{result.message}</p>

        {ok && (
          <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <div>
              <p className="text-xs text-slate-500">Asset / ฝั่ง</p>
              <p className="font-bold">{result.asset} · {result.direction}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">Ticket</p>
              <p className="font-mono text-xs break-all">{result.ticket || "-"}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">Volume</p>
              <p className="font-bold">{result.volume != null ? fmtNum(result.volume, 2) : "-"} lots</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">ขาที่เหลือ (ไม่เปิด)</p>
              <p className="font-bold">{result.remaining_legs ?? "-"} ขา</p>
            </div>
          </div>
        )}

        {!!result.warnings?.length && (
          <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 space-y-1">
            {result.warnings.map((w, i) => (
              <p key={i} className="text-xs text-amber-300 flex items-start gap-1.5">
                <Icon n="warning" size={13} className="mt-0.5 shrink-0" />
                <span>{w}</span>
              </p>
            ))}
          </div>
        )}
        {!!result.rejects?.length && (
          <ul className="text-xs text-loss list-disc pl-4 space-y-1">
            {result.rejects.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        )}
        {!!result.checks?.length && (
          <details className="pt-1">
            <summary className="text-xs text-slate-500 cursor-pointer">Gate checks ({result.checks.length})</summary>
            <ul className="text-xs text-slate-500 list-disc pl-4 pt-1 space-y-0.5">
              {result.checks.map((c, i) => <li key={i}>{c}</li>)}
            </ul>
          </details>
        )}

        <button onClick={onClose}
          className="w-full bg-accent text-white font-semibold rounded px-4 py-2.5 hover:opacity-90 min-h-[44px]">
          ปิด
        </button>
      </div>
    </div>
  );
}
