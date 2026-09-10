"use client";

import { useEffect, useState } from "react";
import Icon from "@/components/Icon";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { MarketCandle, MonitorOpenPosition } from "@/lib/types";

/** จำนวนทศนิยมตามสเกลราคา (ทอง 2, JPY 3, FX 5) */
function priceDigits(p: number): number {
  const a = Math.abs(p);
  if (a >= 100) return 2;
  if (a >= 10) return 3;
  return 5;
}

/** กราฟแท่งเทียน SVG (วาดเอง — ไม่เพิ่ม dependency):
 *  แท่ง daily ~60 แท่ง + เส้น Entry/SL/TP/ราคาปัจจุบัน */
function CandleChart({ candles, entry, sl, tp, current }: {
  candles: MarketCandle[];
  entry: number;
  sl: number | null;
  tp: number | null;
  current: number;
}) {
  const W = 900, H = 700;
  const padL = 72, padR = 112, padT = 26, padB = 26;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const data = candles.slice(-60);
  const n = data.length;
  if (n === 0) return null;

  const levels: { v: number; color: string; label: string; dash: string }[] = [
    { v: entry, color: "#38bdf8", label: "Entry", dash: "" },
  ];
  if (typeof sl === "number" && Number.isFinite(sl))
    levels.push({ v: sl, color: "#ef4444", label: "SL", dash: "5 3" });
  if (typeof tp === "number" && Number.isFinite(tp))
    levels.push({ v: tp, color: "#22c55e", label: "TP", dash: "5 3" });
  if (Number.isFinite(current))
    levels.push({ v: current, color: "#eab308", label: "NOW", dash: "2 3" });

  let lo = Math.min(...data.map((c) => c.l), ...levels.map((l) => l.v));
  let hi = Math.max(...data.map((c) => c.h), ...levels.map((l) => l.v));
  if (!(hi > lo)) { hi = lo + Math.max(Math.abs(lo) * 0.001, 1e-9); }
  const pad = (hi - lo) * 0.08;
  lo -= pad; hi += pad;

  const x = (i: number) => padL + (plotW * (i + 0.5)) / n;
  const y = (p: number) => padT + plotH * (1 - (p - lo) / (hi - lo));
  const bw = Math.max(1.5, (plotW / n) * 0.6);
  const digits = priceDigits((hi + lo) / 2);

  // เส้นตาราง 4 เส้น + ป้ายราคา
  const ticks = [0, 1, 2, 3].map((i) => lo + ((hi - lo) * i) / 3);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto" role="img"
      aria-label={`กราฟแท่งเทียน ${n} แท่ง`}>
      {ticks.map((t, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={y(t)} y2={y(t)}
            stroke="#334155" strokeOpacity={0.5} strokeWidth={1} />
          <text x={padL - 6} y={y(t) + 5} textAnchor="end" fontSize={14}
            fill="#94a3b8">{fmtNum(t, digits)}</text>
        </g>
      ))}
      {data.map((c, i) => {
        const up = c.c >= c.o;
        const color = up ? "#22c55e" : "#ef4444";
        const yO = y(c.o), yC = y(c.c);
        const top = Math.min(yO, yC);
        const hgt = Math.max(1.5, Math.abs(yC - yO));
        return (
          <g key={i}>
            <line x1={x(i)} x2={x(i)} y1={y(c.h)} y2={y(c.l)}
              stroke={color} strokeWidth={1.2} />
            <rect x={x(i) - bw / 2} y={top} width={bw} height={hgt}
              fill={color} fillOpacity={up ? 0.9 : 1} />
          </g>
        );
      })}
      {levels.map((l, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={y(l.v)} y2={y(l.v)}
            stroke={l.color} strokeWidth={2} strokeDasharray={l.dash || undefined} />
          <text x={W - padR + 6} y={y(l.v) + 5} fontSize={14} fontWeight={700}
            fill={l.color}>{l.label} {fmtNum(l.v, digits)}</text>
        </g>
      ))}
      <text x={padL} y={H - 5} fontSize={12} fill="#94a3b8">
        Daily · {n} แท่งย้อนหลัง (Yahoo/สำรอง)
      </text>
    </svg>
  );
}

/**
 * Popup กราฟไม้เปิดค้าง (กดแถวในตาราง Paper) —
 * กราฟแท่งเทียนเต็ม popup + เส้น Entry/SL/TP/ราคาปัจจุบัน
 * (แพทเทิร์นเดียวกับ CloseSingleModal: fixed overlay กลางจอ, render ที่ page root)
 */
export default function PositionChartModal({ position, onClose }: {
  position: MonitorOpenPosition | null;
  onClose: () => void;
}) {
  const [candles, setCandles] = useState<MarketCandle[] | null>(null);
  const [candleErr, setCandleErr] = useState("");

  useEffect(() => {
    if (!position) return;
    setCandles(null);
    setCandleErr("");
    let alive = true;
    (async () => {
      try {
        const c = await api.marketCandles(position.asset, 60);
        if (!alive) return;
        setCandles(c.candles ?? []);
        if ((c.candles ?? []).length === 0 && c.error)
          setCandleErr(c.error);
      } catch (e) {
        if (!alive) return;
        setCandles([]);
        setCandleErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { alive = false; };
  }, [position]);

  if (!position) return null;
  const p = position;
  const win = p.unrealized_pnl >= 0;

  return (
    // โครงเดียวกับ ChatWidget: มือถือเต็มจอ, เดสก์ท็อปกล่องลอยขวาล่าง
    // (กว้างกว่ากล่องแชทเพื่อใส่กราฟแท่งเทียน)
    <div
      className="fixed inset-0 z-50 sm:inset-auto sm:bottom-24 sm:right-6 sm:w-[680px] sm:max-w-[calc(100vw-3rem)] sm:h-[600px] sm:max-h-[78vh] panel flex flex-col shadow-2xl rounded-none sm:rounded-xl safe-top animate-pop"
    >
      {/* ---------- header (แบบเดียวกับกล่อง AI chat) ---------- */}
      <div className="flex items-center justify-between pb-2 border-b border-white/10">
        <p className="text-sm font-semibold flex items-center gap-1.5">
          <Icon n="chart" size={15} />
          {p.asset} · {p.direction === "BUY" ? "▲ BUY" : "▼ SELL"} · {fmtNum(p.volume, 2)} lots
        </p>
        <button onClick={onClose}
          className="w-11 h-11 -mr-2 flex items-center justify-center text-slate-500 hover:text-slate-300 text-lg leading-none active:bg-white/10 rounded-lg"
          aria-label="ปิดหน้าต่าง">✕</button>
      </div>

      <div className="flex-1 space-y-2 overflow-y-auto py-3">

        {/* ---------- PnL headline ---------- */}
        <div className={`rounded-lg p-3 text-center ${win ? "bg-profit/10" : "bg-loss/10"}`}>
          <p className={`text-2xl font-bold ${win ? "text-profit" : "text-loss"}`}>
            {win ? "+" : ""}${fmtNum(p.unrealized_pnl, 2)}
          </p>
          <p className="text-xs text-slate-500 mt-0.5">
            PnL (ยังไม่ปิด) · R {fmtNum(p.r_multiple ?? 0, 2)} · เสี่ยงถ้าโดน SL ${fmtNum(p.risk_amount ?? 0, 2)}
          </p>
        </div>

        {/* ---------- กราฟเต็ม popup ---------- */}
        <div className="rounded-xl border border-white/10 bg-black/30 p-1 sm:p-2">
          {candles === null && (
            <div className="flex items-center justify-center gap-2 py-16 text-slate-400 text-sm">
              <Icon n="spinner" size={16} className="animate-spin" /> กำลังโหลดกราฟ…
            </div>
          )}
          {candles !== null && candles.length > 0 && (
            <>
              <CandleChart candles={candles} entry={p.entry_price}
                sl={p.stop_loss} tp={p.take_profit} current={p.current_price} />
              <div className="flex flex-wrap gap-x-4 gap-y-1 px-1 pb-1 text-sm text-slate-300">
                <span><span className="text-[#38bdf8] font-bold">—</span> Entry</span>
                <span><span className="text-loss font-bold">- -</span> SL</span>
                <span><span className="text-profit font-bold">- -</span> TP</span>
                <span><span className="text-yellow-400 font-bold">··</span> ราคาปัจจุบัน</span>
              </div>
            </>
          )}
          {candles !== null && candles.length === 0 && (
            <p className="text-center text-sm text-slate-500 py-10">
              โหลดกราฟไม่สำเร็จ{candleErr ? ` (${candleErr})` : ""} — ยังดูรายละเอียดไม้ด้านล่างได้
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
