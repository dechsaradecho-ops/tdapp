"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import LoadingGraphic from "@/components/LoadingGraphic";
import { scoreColor } from "@/lib/format";
import { AssetOpportunity } from "@/lib/types";

const BAND_LABEL: Record<string, string> = {
  very_high: "Very High",
  high: "High",
  medium: "Medium",
  low: "Low",
};

/** แถวเดียวของ Opportunity Score — กดที่แถวเพื่อเปิด popup "ที่มาของคะแนน"
 *  (รายละเอียดการคำนวณทุก component จาก score_reasons ที่ scanner เขียนลง DB).
 *  popover แบบ glass ต้อง createPortal ลง document.body เพราะ .panel มี
 *  backdrop-filter ที่ทำให้ position: fixed ภายใน panel ถูก trap
 *  (pattern เดียวกับ LevelMovedBadge หน้า monitor). */
function ScoreRow({ o, gate, tradable }: {
  o: AssetOpportunity;
  gate: number | null;
  tradable: boolean;
}) {
  const [pop, setPop] = useState(false);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  const place = useCallback(() => {
    const row = rowRef.current, popEl = popRef.current;
    if (!row) return;
    const r = row.getBoundingClientRect();
    const pw = popEl?.offsetWidth ?? 320;
    const ph = popEl?.offsetHeight ?? 220;
    let left = r.left + r.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    let top = r.bottom + 8;                                       // ใต้แถวเป็นค่าเริ่มต้น
    if (top + ph > window.innerHeight - 8) top = r.top - ph - 8;  // ล่างไม่พอ → เหนือแถว
    if (top < 8) top = 8;
    setPos({ top, left });
  }, []);

  useEffect(() => {
    if (!pop) return;
    place();
    const close = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node | null;
      if (t && (rowRef.current?.contains(t) || popRef.current?.contains(t))) return;
      setPop(false);
    };
    // scroll ภายในรายการเหตุผลของ popup ต้องไม่ปิด popup — ปิดเฉพาะ scroll นอก popup
    const onScroll = (e: Event) => {
      const t = e.target as Node | null;
      if (t && popRef.current?.contains(t)) return;
      setPop(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("touchstart", close, { passive: true });
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("touchstart", close);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [pop, place]);

  const passes = gate != null && o.score >= gate;
  const details = (o.score_reasons?.length ? o.score_reasons : o.reasons).filter(Boolean);

  return (
    <div
      ref={rowRef}
      role="button"
      tabIndex={0}
      aria-expanded={pop}
      aria-label={`Opportunity Score ${o.asset} — แตะเพื่อดูรายละเอียดการคำนวณ`}
      onClick={() => setPop((v) => !v)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setPop((v) => !v); }
      }}
      className="border-b border-slate-800 pb-2 last:border-0 cursor-pointer touch-manipulation select-none"
    >
      <div className="flex items-center justify-between">
        <span className="font-semibold">
          {o.asset} <span className="text-xs font-normal text-slate-500">{BAND_LABEL[o.band] ?? o.band}</span>
          {!tradable && (
            <span className="text-[10px] text-slate-500 border border-white/10 rounded px-1 ml-1.5">ดูอย่างเดียว</span>
          )}
        </span>
        <span className="flex items-center gap-1">
          <span className={`font-bold ${scoreColor(o.score)}`}>{o.score.toFixed(0)}%</span>
          <span className={`text-slate-600 transition-transform ${pop ? "rotate-90" : ""}`}>›</span>
        </span>
      </div>
      <div className="relative h-2 bg-slate-800 rounded mt-1 overflow-hidden">
        <div
          className={`h-full rounded ${o.score >= 81 ? "bg-emerald-500" : o.score >= 61 ? "bg-accent" : o.score >= 31 ? "bg-amber-500" : "bg-slate-600"}`}
          style={{ width: `${o.score}%` }}
        />
        {gate != null && (
          <div
            className="absolute top-0 h-2 w-0.5 bg-rose-400/80"
            style={{ left: `${Math.min(100, Math.max(0, gate))}%` }}
            title={`Min Confidence ${gate}%`}
          />
        )}
      </div>
      <div className="flex justify-between text-xs text-slate-500 mt-1">
        <span>
          Confidence {o.score.toFixed(0)}%
          {gate != null && (
            <span className={passes ? "text-emerald-400 ml-1.5" : "text-rose-400 ml-1.5"}>
              ({gate}% {passes ? "ผ่านเกณฑ์" : "ต่ำกว่าเกณฑ์"})
            </span>
          )}
        </span>
        <span>{o.reasons[0]?.slice(0, 60) ?? ""}</span>
      </div>
      {pop && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label={`รายละเอียดการคำนวณคะแนน ${o.asset}`}
          style={{ position: "fixed", top: pos.top, left: pos.left, width: "min(340px, calc(100vw - 16px))" }}
          className="z-50 rounded-xl border border-slate-700 bg-slate-900/95 backdrop-blur px-3.5 py-3 shadow-xl"
        >
          <div className="flex items-center justify-between">
            <span className="font-semibold text-slate-100">
              {o.asset} <span className="text-xs font-normal text-slate-500">{BAND_LABEL[o.band] ?? o.band}</span>
            </span>
            <span className={`font-bold ${scoreColor(o.score)}`}>{o.score.toFixed(0)}%</span>
          </div>
          <div className="mt-1 text-xs">
            {gate != null ? (
              <span className={passes ? "text-emerald-400" : "text-rose-400"}>
                เกณฑ์ Min Confidence {gate}% — {passes ? "ผ่านเกณฑ์" : "ต่ำกว่าเกณฑ์"}
              </span>
            ) : (
              <span className="text-slate-500">ยังไม่ได้ตั้งเกณฑ์ Min Confidence</span>
            )}
            {!tradable && (
              <span className="block text-slate-500 mt-0.5">อยู่นอก allowed_assets — วิเคราะห์อย่างเดียว ไม่เข้าระบบเทรด</span>
            )}
          </div>
          <div className="border-t border-slate-800 my-2" />
          <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 mb-1">ที่มาของคะแนน</p>
          {details.length ? (
            <ul className="space-y-1 text-xs text-slate-300 max-h-[45vh] overflow-y-auto pr-1">
              {details.map((r, i) => (
                <li key={i} className="flex gap-1.5">
                  <span className="text-slate-600 shrink-0">•</span>
                  <span className="whitespace-pre-line">{r}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-slate-500">ยังไม่มีรายละเอียด — รอ Market Scanner รอบถัดไป</p>
          )}
          <p className="mt-2 text-[10px] text-slate-600">
            คะแนนรวมน้ำหนัก Trend / Momentum / Volatility / ข่าว — Market Scanner อัปเดตทุก 5 นาที
          </p>
        </div>,
        document.body
      )}
    </div>
  );
}

export default function OpportunityScore({ opportunities, loading, error, minConfidence, minConfidenceGold, tradableAssets }: {
  opportunities: AssetOpportunity[];
  loading?: boolean;
  error?: boolean;
  /** Gate threshold (trading_settings.min_confidence) — badge pass/fail per symbol when provided. */
  minConfidence?: number;
  /** Gold-specific gate (min_confidence_gold) — applied to XAUUSD when set; null/undefined = use base gate. */
  minConfidenceGold?: number | null;
  /** Trading whitelist (allowed_assets) — symbols outside it are analysis-only
   * ("ดูอย่างเดียว"); undefined/empty = all tradable (settings not loaded). */
  tradableAssets?: string[];
}) {
  // Client-side paging (หน้าละ 5) — 28 คู่ใน universe ยาวเกินไปสำหรับการอ่านครั้งเดียว
  const PAGE_SIZE = 5;
  const [page, setPage] = useState(1);
  if (loading) {
    return <LoadingGraphic message="กำลังโหลดข้อมูลตลาด... (Render cold start อาจใช้เวลาสักครู่)" />;
  }
  if (!opportunities.length) {
    return error
      ? <p className="text-amber-400 text-sm">โหลดข้อมูลไม่สำเร็จ — รีเฟรชหน้าเพื่อลองอีกครั้ง</p>
      : <p className="text-slate-500 text-sm">ยังไม่มีข้อมูล — รอ Market Scanner ทำงานก่อน</p>;
  }
  const gateFor = (asset: string): number | null => {
    if (asset === "XAUUSD" && minConfidenceGold != null) return minConfidenceGold;
    return minConfidence ?? null;
  };
  const isTradable = (asset: string): boolean =>
    !tradableAssets?.length || tradableAssets.includes(asset);
  const totalPages = Math.max(1, Math.ceil(opportunities.length / PAGE_SIZE));
  // clamp แทน reset — dashboard poll ทุก 30 วิ อย่าบังคับผู้ใช้กลับหน้า 1 ตอนข้อมูลรีเฟรช
  const safePage = Math.min(page, totalPages);
  const pageRows = opportunities.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);
  return (
    <div>
    <div className="space-y-3">
      {pageRows.map((o) => (
        <ScoreRow key={o.asset} o={o} gate={gateFor(o.asset)} tradable={isTradable(o.asset)} />
      ))}
    </div>
    {opportunities.length > 0 && (
      <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
        <p className="text-xs text-slate-500">
          หน้า {safePage}/{totalPages} · แสดง {pageRows.length} จาก {opportunities.length} รายการ
        </p>
        {totalPages > 1 && (
          <div className="flex items-center gap-2">
            <button onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={safePage <= 1}
              className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
              ก่อนหน้า
            </button>
            <span className="text-xs text-slate-400">{safePage} / {totalPages}</span>
            <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={safePage >= totalPages}
              className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
              ถัดไป
            </button>
          </div>
        )}
      </div>
    )}
    </div>
  );
}
