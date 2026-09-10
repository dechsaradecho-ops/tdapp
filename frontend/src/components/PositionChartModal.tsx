"use client";

import { useEffect, useState } from "react";
import Icon from "@/components/Icon";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { MarketCandle, MonitorOpenPosition } from "@/lib/types";

/** Built-in spreads (mirror of backend DEFAULT_SPREADS + settings page) —
 *  ใช้เมื่อไม่มี override รายสัญลักษณ์ใน settings */
const DEFAULT_SPREADS: Record<string, number> = {
  EURUSD: 0.00010, GBPUSD: 0.00015, USDJPY: 0.015,
  AUDUSD: 0.00015, NZDUSD: 0.00020, USDCAD: 0.00020, USDCHF: 0.00015,
  EURGBP: 0.00020, EURJPY: 0.020, EURAUD: 0.00025, EURNZD: 0.00035,
  EURCAD: 0.00025, EURCHF: 0.00020,
  GBPJPY: 0.030, GBPAUD: 0.00035, GBPNZD: 0.00045, GBPCAD: 0.00035,
  GBPCHF: 0.00030,
  AUDJPY: 0.025, AUDNZD: 0.00035, AUDCAD: 0.00025, AUDCHF: 0.00025,
  NZDJPY: 0.025, NZDCAD: 0.00030,
  CADJPY: 0.030, CADCHF: 0.00030, CHFJPY: 0.030,
  XAUUSD: 0.30,
};

/** จำนวนทศนิยมตามสเกลราคา (ทอง 2, JPY 3, FX 5) */
function priceDigits(p: number): number {
  const a = Math.abs(p);
  if (a >= 100) return 2;
  if (a >= 10) return 3;
  return 5;
}

function fmtP(p: number | null | undefined): string {
  if (typeof p !== "number" || !Number.isFinite(p)) return "-";
  return fmtNum(p, priceDigits(p));
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
  const W = 800, H = 400;
  const padL = 56, padR = 78, padT = 10, padB = 18;
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
          <text x={padL - 4} y={y(t) + 3} textAnchor="end" fontSize={10}
            fill="#64748b">{fmtNum(t, digits)}</text>
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
            stroke={l.color} strokeWidth={1.4} strokeDasharray={l.dash || undefined} />
          <text x={W - padR + 4} y={y(l.v) + 3} fontSize={10} fontWeight={700}
            fill={l.color}>{l.label} {fmtNum(l.v, digits)}</text>
        </g>
      ))}
      <text x={padL} y={H - 4} fontSize={10} fill="#64748b">
        Daily · {n} แท่งย้อนหลัง (Yahoo/สำรอง)
      </text>
    </svg>
  );
}

/**
 * Popup กราฟไม้เปิดค้าง (กดแถวในตาราง Paper) —
 * กราฟแท่งเทียนใหญ่ + เส้น Entry/SL/TP/ราคาปัจจุบัน + ราคา/spread/buy-sell
 * (แพทเทิร์นเดียวกับ CloseSingleModal: fixed overlay กลางจอ, render ที่ page root)
 */
export default function PositionChartModal({ position, onClose }: {
  position: MonitorOpenPosition | null;
  onClose: () => void;
}) {
  const [candles, setCandles] = useState<MarketCandle[] | null>(null);
  const [candleErr, setCandleErr] = useState("");
  const [spread, setSpread] = useState<number | null>(null);

  useEffect(() => {
    if (!position) return;
    setCandles(null);
    setCandleErr("");
    setSpread(null);
    let alive = true;
    (async () => {
      // กราฟ + spread โหลดพร้อมกัน (settings ให้ effective spread)
      const [c, s] = await Promise.allSettled([
        api.marketCandles(position.asset, 60),
        api.getSettings(),
      ]);
      if (!alive) return;
      if (c.status === "fulfilled") {
        setCandles(c.value.candles ?? []);
        if ((c.value.candles ?? []).length === 0 && c.value.error)
          setCandleErr(c.value.error);
      } else {
        setCandleErr(c.reason instanceof Error ? c.reason.message : String(c.reason));
      }
      if (s.status === "fulfilled") {
        const a = position.asset.toUpperCase();
        const ov = s.value.spread_overrides?.[a];
        const eff = (typeof ov === "number" && Number.isFinite(ov))
          ? ov
          : (DEFAULT_SPREADS[a] ?? s.value.paper_spread ?? 0);
        setSpread(Math.max(0, eff));
      }
    })();
    return () => { alive = false; };
  }, [position]);

  if (!position) return null;
  const p = position;
  const win = p.unrealized_pnl >= 0;
  const bid = spread != null ? p.current_price - spread / 2 : null;
  const ask = spread != null ? p.current_price + spread / 2 : null;
  const srcLabel = p.price_source === "spot" ? "spot สด"
    : p.price_source === "daily" ? "daily close"
    : p.price_source === "broker" ? "broker"
    : p.price_source === "entry" ? "entry (ไม่มี feed)" : (p.price_source || "-");

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade"
      style={{ background: "rgba(0,0,0,0.7)", WebkitBackdropFilter: "blur(16px) saturate(140%)", backdropFilter: "blur(16px) saturate(140%)" }}
      onClick={onClose}
    >
      <div
        className="panel w-full max-w-4xl space-y-3 animate-pop max-h-[94vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ---------- header ---------- */}
        <div className="flex items-center justify-between">
          <h3 className="panel-title flex items-center gap-1.5">
            <Icon n="chart" size={16} className="text-accent" />
            {p.asset} · {p.direction === "BUY" ? "▲ BUY" : "▼ SELL"} · {fmtNum(p.volume, 2)} lots
          </h3>
          <button onClick={onClose} aria-label="ปิดหน้าต่าง"
            className="text-slate-400 hover:text-accent text-lg leading-none min-h-[44px] min-w-[44px]">
            ✕
          </button>
        </div>

        {/* ---------- PnL headline ---------- */}
        <div className={`rounded-lg p-3 text-center ${win ? "bg-profit/10" : "bg-loss/10"}`}>
          <p className={`text-2xl font-bold ${win ? "text-profit" : "text-loss"}`}>
            {win ? "+" : ""}${fmtNum(p.unrealized_pnl, 2)}
          </p>
          <p className="text-xs text-slate-500 mt-0.5">
            PnL (ยังไม่ปิด) · R {fmtNum(p.r_multiple ?? 0, 2)} · เสี่ยงถ้าโดน SL ${fmtNum(p.risk_amount ?? 0, 2)}
          </p>
        </div>

        {/* ---------- กราฟ ---------- */}
        <div className="rounded-xl border border-white/10 bg-black/30 p-2">
          {candles === null && (
            <div className="flex items-center justify-center gap-2 py-16 text-slate-400 text-sm">
              <Icon n="spinner" size={16} className="animate-spin" /> กำลังโหลดกราฟ…
            </div>
          )}
          {candles !== null && candles.length > 0 && (
            <>
              <CandleChart candles={candles} entry={p.entry_price}
                sl={p.stop_loss} tp={p.take_profit} current={p.current_price} />
              <div className="flex flex-wrap gap-x-3 gap-y-1 px-1 pb-1 text-[11px] text-slate-400">
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

        {/* ---------- ราคา / spread / buy-sell ---------- */}
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-2 text-sm">
          <div><p className="text-xs text-slate-500">Entry</p><p className="font-bold">{fmtP(p.entry_price)}</p></div>
          <div><p className="text-xs text-slate-500">ราคาปัจจุบัน ({srcLabel})</p><p className="font-bold">{fmtP(p.current_price)}</p></div>
          <div><p className="text-xs text-slate-500">SL / TP</p><p className="font-bold text-xs">{fmtP(p.stop_loss)} / {fmtP(p.take_profit)}</p></div>
          <div><p className="text-xs text-slate-500">Spread (bid-ask เต็ม)</p><p className="font-bold">{spread == null ? "…" : fmtNum(spread, priceDigits(spread) === 2 ? 2 : 5)}</p></div>
          <div><p className="text-xs text-slate-500">Buy (ask)</p><p className="font-bold text-profit">{bid != null && ask != null ? fmtP(ask) : "-"}</p></div>
          <div><p className="text-xs text-slate-500">Sell (bid)</p><p className="font-bold text-loss">{bid != null && ask != null ? fmtP(bid) : "-"}</p></div>
          <div><p className="text-xs text-slate-500">Ticket</p><p className="font-mono text-xs break-all">{p.ticket || "-"}</p></div>
          <div><p className="text-xs text-slate-500">ที่มา</p><p className="font-bold">{p.source === "auto" ? "Auto" : "Approve"}</p></div>
          <div><p className="text-xs text-slate-500">เปิดเมื่อ</p><p className="font-bold text-xs">{p.created_at ? new Date(p.created_at).toLocaleString("th-TH", { dateStyle: "short", timeStyle: "short" }) : "-"}</p></div>
        </div>

        <p className="text-[11px] text-slate-600 text-center">แตะนอกกรอบเพื่อปิด · ราคา bid/ask = mid ± spread/2</p>
      </div>
    </div>
  );
}
