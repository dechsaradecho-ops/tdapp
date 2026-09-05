"use client";

import { ClosePositionResult } from "@/lib/types";
import { fmtNum } from "@/lib/format";

/** Confirmation popup after a manual close — full trade summary + portfolio
 * stats returned by POST /api/trading/positions/close (no second round-trip). */
export default function ClosePositionModal({
  result,
  onClose,
}: {
  result: ClosePositionResult | null;
  onClose: () => void;
}) {
  if (!result) return null;
  const win = result.pnl >= 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.7)", WebkitBackdropFilter: "blur(16px) saturate(140%)", backdropFilter: "blur(16px) saturate(140%)" }}
      onClick={onClose}
    >
      <div
        className="panel w-full max-w-md space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ---------- header ---------- */}
        <div className="flex items-center justify-between">
          <h3 className="panel-title">
            {win ? "✅ ปิดไม้สำเร็จ (กำไร)" : "🔻 ปิดไม้สำเร็จ (ขาดทุน)"}
          </h3>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-accent text-lg leading-none"
            aria-label="ปิดหน้าต่าง"
          >
            ✕
          </button>
        </div>

        {/* ---------- headline PnL ---------- */}
        <div className={`rounded-lg p-4 text-center ${win ? "bg-profit/10" : "bg-loss/10"}`}>
          <p className="text-xs text-slate-400">กำไร/ขาดทุนสุทธิ (PnL)</p>
          <p className={`text-3xl font-bold ${win ? "text-profit" : "text-loss"}`}>
            {win ? "+" : ""}${fmtNum(result.pnl, 2)}
          </p>
          <p className={`text-sm ${win ? "text-profit" : "text-loss"}`}>
            {result.pnl_pct >= 0 ? "+" : ""}
            {fmtNum(result.pnl_pct, 2)}% ของทุน
          </p>
        </div>

        {/* ---------- trade details ---------- */}
        <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <Detail label="Asset" value={result.asset} />
          <Detail
            label="ฝั่ง"
            value={
              <span className={result.direction === "BUY" ? "text-profit" : "text-loss"}>
                {result.direction === "BUY" ? "▲ BUY" : "▼ SELL"}
              </span>
            }
          />
          <Detail label="Lots" value={fmtNum(result.volume, 2)} />
          <Detail label="Ticket" value={result.ticket || "-"} />
          <Detail label="ราคาเข้า (Entry)" value={fmtNum(result.entry_price, 5)} />
          <Detail label="ราคาปิด (Exit)" value={fmtNum(result.exit_price, 5)} />
          <Detail
            label="ระยะเวลาถือ"
            value={
              result.holding_time_min != null
                ? result.holding_time_min >= 60
                  ? `${fmtNum(result.holding_time_min / 60, 1)} ชม.`
                  : `${fmtNum(result.holding_time_min, 0)} นาที`
                : "-"
            }
          />
          <Detail label="เหตุผลปิด" value="✋ ปิดเอง (Manual)" />
        </div>

        {/* ---------- portfolio summary ---------- */}
        <div className="border-t border-slate-800 pt-3">
          <p className="text-xs text-slate-500 uppercase tracking-wide mb-2">
            สรุปพอร์ตหลังปิดไม้
          </p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <Detail label="ไม้ที่เปิดค้างเหลือ" value={`${result.remaining_open} ไม้`} />
            <Detail
              label="PnL วันนี้"
              value={
                <span className={result.pnl_today >= 0 ? "text-profit" : "text-loss"}>
                  {result.pnl_today >= 0 ? "+" : ""}${fmtNum(result.pnl_today, 2)}
                </span>
              }
            />
            <Detail
              label="PnL รวม (ปิดแล้ว)"
              value={
                <span className={result.total_realized_pnl >= 0 ? "text-profit" : "text-loss"}>
                  {result.total_realized_pnl >= 0 ? "+" : ""}${fmtNum(result.total_realized_pnl, 2)}
                </span>
              }
            />
            <Detail label="สถิติ" value={`ชนะ ${result.wins} / แพ้ ${result.losses}`} />
          </div>
        </div>

        {result.warnings.length > 0 && (
          <p className="text-xs text-amber-400">
            ⚠️ {result.warnings.join("; ")}
          </p>
        )}

        {/* ---------- close button ---------- */}
        <button
          onClick={onClose}
          className="w-full bg-accent text-surface font-semibold rounded px-4 py-2.5 hover:opacity-90"
        >
          ปิดหน้าต่างนี้
        </button>
      </div>
    </div>
  );
}

function Detail({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs text-slate-500">{label}</p>
      <p className="font-semibold">{value}</p>
    </div>
  );
}
