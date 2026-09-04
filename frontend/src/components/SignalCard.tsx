"use client";

import { useState } from "react";
import { API_BASE, SignalProposal } from "@/lib/types";
import { fmtNum } from "@/lib/format";
import LimitLevels from "@/components/LimitLevels";
import ReasonList from "@/components/ReasonList";

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
      <div className="grid grid-cols-2 gap-2 text-sm mb-2">
        <Field label="Confidence" value={`${signal.confidence}%`} />
        <Field label="Risk / Trade" value={`${signal.risk_per_trade_pct}%`} />
        <Field label="Entry" value={fmtNum(signal.entry, 5)} />
        <Field label="RR" value={`1 : ${signal.expected_rr}`} />
        <Field label="Stop Loss" value={fmtNum(signal.stop_loss, 5)} />
        <Field label="Take Profit" value={fmtNum(signal.take_profit, 5)} />
      </div>
      {signal.live_price != null && signal.live_price > 0 && (
        // ราคาตลาดปัจจุบัน (spot feed) เทียบกับ entry บนการ์ด — ถ้า entry
        // ห่างจากราคาสดมาก ผู้ใช้เห็นทันทีว่า entry เป็นราคาเก่า (daily close)
        // แทนที่จะดูเหมือนราคาปัจจุบัน
        <div className="mb-2 flex items-center gap-2 rounded border border-accent/30 bg-accent/5 px-2 py-1 text-xs">
          <span className="text-slate-400">ราคาตลาดตอนนี้</span>
          <span className="font-semibold text-accent">{fmtNum(signal.live_price, 5)}</span>
          {signal.entry > 0 && (
            <span className={liveDeltaPct >= 0 ? "text-profit" : "text-loss"}>
              {liveDeltaPct >= 0 ? "▲" : "▼"} {Math.abs(liveDeltaPct).toFixed(2)}%
            </span>
          )}
        </div>
      )}
      {signal.approval !== "approved" && signal.expires_min_left != null && (
        // นับถอยหลัง: อีกกี่นาทีก่อนสัญญาณหมดอายุและระบบเริ่มประเมินใหม่
        // (TTL 30 นาที) — เหลือ <10 นาทีเปลี่ยนเป็นสีเตือน
        <div className={`mb-2 flex items-center gap-2 rounded border px-2 py-1 text-xs ${
          signal.expires_min_left < 10
            ? "border-amber-500/40 bg-amber-500/10 text-amber-400"
            : "border-slate-700 bg-surface text-slate-400"
        }`}>
          <span>⏳</span>
          <span>
            อีก {Math.max(signal.expires_min_left, 0).toFixed(0)} นาที ระบบจะหมดอายุและเริ่มประเมินใหม่
          </span>
        </div>
      )}
      <LimitLevels signal={signal} />
      {/* เหตุผลจัดหมวดหมู่ (เทรนด์/โมเมนตัม/ผันผวน/ข่าว) — แต่ละหมวด toggle พับ/กางได้ */}
      <ReasonList reasons={signal.reason} />
      {signal.approval === "approved" ? (
        // อนุมัติแล้ว/ยิงแล้ว — แสดงสแตมป์เวลาแทนปุ่ม
        <div className="mt-3 flex items-center gap-2 rounded border border-profit/40 bg-profit/10 px-2 py-1.5 text-xs text-profit">
          <span>{isAuto ? "🤖 ยิงออเดอร์แล้ว" : "✓ อนุมัติแล้ว"}</span>
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
          <span>🤖 พร้อมยิง — ระบบจะเปิดออเดอร์ให้ภายใน ~1 นาที</span>
        </div>
      ) : (
        <div className="mt-3 flex gap-2">
          <button disabled={approving} onClick={() => decide(true)}
            className="flex-1 bg-profit text-surface font-semibold rounded-lg py-2.5 text-sm min-h-[44px] active:brightness-90 disabled:opacity-50 transition">
            Approve
          </button>
          <button disabled={approving} onClick={() => decide(false)}
            className="flex-1 border border-loss text-loss rounded-lg py-2.5 text-sm min-h-[44px] active:bg-loss/10 disabled:opacity-50 transition">
            Reject
          </button>
        </div>
      )}
      {done && <p className="text-xs text-slate-500 mt-2 break-all">{done}</p>}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-surface rounded p-2 border border-slate-800">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="font-semibold">{value}</p>
    </div>
  );
}
