"use client";

import { useState } from "react";
import { API_BASE, SignalProposal } from "@/lib/types";
import { fmtNum } from "@/lib/format";
import CopyNum from "@/components/CopyNum";
import Icon from "@/components/Icon";
import SignalLevels from "@/components/SignalLevels";
import ReasonList from "@/components/ReasonList";
import CalcNotes from "@/components/CalcNotes";

const DECISION_STYLE: Record<string, string> = {
  "TRADE": "bg-profit/20 text-profit border-profit",
  "WAIT": "bg-amber-500/20 text-amber-400 border-amber-500",
  "REDUCE RISK": "bg-loss/20 text-loss border-loss",
  "INCREASE CASH": "bg-slate-500/20 text-slate-300 border-slate-500",
};

export default function SignalCard({ signal, orderMode }: { signal: SignalProposal; orderMode?: string }) {
  const [approving, setApproving] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const isAuto = orderMode === "auto";
  // % gap between the live market price and the card's entry — positive
  // means the market has moved UP past the entry (entry is stale/behind).
  const liveDeltaPct = signal.live_price && signal.entry > 0
    ? (signal.live_price - signal.entry) / signal.entry * 100
    : 0;

  const decide = async (approve: boolean) => {
    setApproving(true);
    try {
      // DEMO signal ids are local; a real signal_id comes from the DB row.
      const res = await fetch(`${API_BASE}/api/signals/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ signal_id: signal.asset, approve }),
      });
      setDone(await res.text());
    } catch (e) {
      setDone(String(e));
    } finally {
      setApproving(false);
    }
  };

  return (
    <div className="panel">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="font-bold text-lg">{signal.asset}</span>
          <span className={`px-2 py-0.5 rounded text-xs font-bold ${signal.direction === "BUY" ? "bg-profit text-surface" : "bg-loss text-white"}`}>
            {signal.direction}
          </span>
        </div>
        <span className={`text-xs px-2 py-1 rounded border ${DECISION_STYLE[signal.recommendation] ?? ""}`}>
          {signal.recommendation}
        </span>
      </div>
      {/* เมตาแถวเดียว: มั่นใจ • RR • เสี่ยง • นับถอยหลัง — แทน grid 2 แถว + แถว TTL */}
      <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-400">
        <span>มั่นใจ <b className="text-slate-200">{signal.confidence}%</b></span>
        <span className="text-slate-600">•</span>
        <span>RR <b className="text-slate-200">1 : {signal.expected_rr}</b></span>
        <span className="text-slate-600">•</span>
        <span>เสี่ยง {signal.risk_per_trade_pct}%/ไม้</span>
        {signal.approval !== "approved" && signal.expires_min_left != null && (
          <>
            <span className="text-slate-600">•</span>
            <span className={signal.expires_min_left < 10 ? "text-amber-400 font-semibold" : ""}>
              ⏳ อีก {Math.max(signal.expires_min_left, 0).toFixed(0)} นาที
            </span>
          </>
        )}
      </div>
      {/* Entry/SL/TP แถวเดียว 3 คอลัมน์ (เดิม 2 คอลัมน์ 3 แถว) — pill สีแดง/เขียว */}
      <div className="grid grid-cols-3 gap-1.5 text-sm mb-2">
        <Field compact label="Entry" value={<CopyNum value={signal.entry} />} />
        <Field compact label="SL" value={<span className="inline-flex items-center rounded-full bg-loss/30 text-loss px-2 py-0.5"><CopyNum value={signal.stop_loss} /></span>} />
        <Field compact label="TP" value={<span className="inline-flex items-center rounded-full bg-profit/30 text-profit px-2 py-0.5"><CopyNum value={signal.take_profit} /></span>} />
      </div>
      {/* ราคาสด + ขนาดไม้ แถวเดียว 2 คอลัมน์ (เดิมแถวเต็ม 2 แถว) */}
      <div className="mb-2 grid grid-cols-2 gap-1.5 text-xs">
        {signal.live_price != null && signal.live_price > 0 ? (
          <div className="flex min-w-0 items-center gap-1.5 rounded-lg border border-accent/30 bg-accent/5 px-2 py-1.5">
            <span className="shrink-0 text-slate-400">สด</span>
            <span className="truncate font-semibold text-accent">{fmtNum(signal.live_price, 5)}</span>
            {signal.entry > 0 && (
              <span className={`inline-flex shrink-0 items-center rounded-full px-1.5 py-0.5 font-semibold ${liveDeltaPct >= 0 ? "bg-profit/30 text-profit" : "bg-loss/30 text-loss"}`}>
                {liveDeltaPct >= 0 ? "▲" : "▼"}{Math.abs(liveDeltaPct).toFixed(1)}%
              </span>
            )}
          </div>
        ) : (
          <div className="flex min-w-0 items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-2 py-1.5 text-slate-500">
            สด —
          </div>
        )}
        <div className="flex min-w-0 items-center gap-1.5 rounded-lg border border-accent/30 bg-accent/5 px-2 py-1.5">
          <span className="shrink-0 text-slate-400">ไม้</span>
          {signal.suggested_lots != null ? (
            <span className="inline-flex items-center rounded-full bg-accent/30 text-accent px-2 py-0.5 font-semibold">{signal.suggested_lots.toFixed(2)} lots</span>
          ) : (
            <span className="text-slate-500">—</span>
          )}
        </div>
      </div>
      {/* SL/TP ด้านบนคือค่า effective (tier + SL cap = ที่ระบบจะยิงจริง) — 3 ระดับล่างรวมในพับเดียว */}
      <SignalLevels signal={signal} />
      {/* เหตุผลจัดหมวดหมู่ (เทรนด์/โมเมนตัม/ผันผวน/ข่าว) — แต่ละหมวด toggle พับ/กางได้ */}
      <ReasonList reasons={signal.reason} />
      {/* ขั้นตอนคำนวณทีละขั้น (SL/TP/ขนาดไม้/สเปรด) — ไม่เหลือตัวเลขลอยๆ */}
      <CalcNotes notes={signal.calc_notes} />
      {signal.approval === "approved" ? (
        // อนุมัติแล้ว/ยิงแล้ว — แสดงสแตมป์เวลาแทนปุ่ม
        <div className="mt-3 flex items-center gap-2 rounded border border-profit/40 bg-profit/10 px-2 py-1.5 text-xs text-profit">
          <span className="inline-flex items-center gap-1.5">{isAuto ? <>ยิงออเดอร์แล้ว</> : <>อนุมัติแล้ว</>}</span>
          <span className="text-slate-400">
            {new Date(
              signal.approved_at ?? signal.created_at ?? ""
            ).toLocaleString("th-TH")}
          </span>
        </div>
      ) : signal.order_blocked ? (
        // ถึง limit แล้ว — สัญญาณยัง generate ต่อทุกวัน แต่ยังไม่เปิดออเดอร์
        <div className="mt-3 flex items-center gap-2 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-xs text-amber-400">
          <span>⏸ {signal.order_blocked}</span>
        </div>
      ) : isAuto ? (
        // โหมด auto — ไม่มีปุ่มให้กด: auto trader จะยิงเองผ่าน gate ทั้งหมด
        <div className="mt-3 flex items-center gap-2 rounded border border-accent/40 bg-accent/10 px-2 py-1.5 text-xs text-accent">
          <span className="inline-flex items-center gap-1.5"><Icon n="bot" size={13} /> พร้อมยิง — ระบบจะเปิดออเดอร์ให้ภายใน ~1 นาที</span>
        </div>
      ) : (
        <div className="mt-3 flex gap-2">
          <button disabled={approving} onClick={() => decide(true)}
            className="flex-1 bg-profit text-white font-semibold rounded-lg py-2.5 text-sm min-h-[44px] active:brightness-90 disabled:opacity-50 transition">
            Approve
          </button>
          <button disabled={approving} onClick={() => decide(false)}
            className="flex-1 bg-loss text-white font-semibold rounded-lg py-2.5 text-sm min-h-[44px] active:brightness-90 disabled:opacity-50 transition">
            Reject
          </button>
        </div>
      )}
      {done && <p className="text-xs text-slate-500 mt-2 break-all">{done}</p>}
    </div>
  );
}

function Field({ label, value, compact = false }: { label: string; value: React.ReactNode; compact?: boolean }) {
  return (
    <div className={`bg-white/[0.05] border border-white/10 ${compact ? "rounded-lg px-1.5 py-1 min-w-0" : "rounded-xl p-2"}`}>
      <p className="text-[11px] text-slate-500 truncate">{label}</p>
      <p className={`font-bold ${compact ? "text-[13px] truncate" : ""}`}>{value}</p>
    </div>
  );
}
