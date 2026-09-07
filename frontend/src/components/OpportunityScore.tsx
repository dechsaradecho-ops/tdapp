"use client";

import { useState } from "react";
import { scoreColor } from "@/lib/format";
import { AssetOpportunity } from "@/lib/types";

const BAND_LABEL: Record<string, string> = {
  very_high: "Very High",
  high: "High",
  medium: "Medium",
  low: "Low",
};

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
    return <p className="text-slate-500 text-sm animate-pulse">⏳ กำลังโหลดข้อมูลตลาด... (Render cold start อาจใช้เวลาสักครู่)</p>;
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
      {pageRows.map((o) => {
        const gate = gateFor(o.asset);
        const passes = gate != null && o.score >= gate;
        const tradable = isTradable(o.asset);
        return (
        <div key={o.asset} className="border-b border-slate-800 pb-2 last:border-0">
          <div className="flex items-center justify-between">
            <span className="font-semibold">
              {o.asset} <span className="text-xs font-normal text-slate-500">{BAND_LABEL[o.band] ?? o.band}</span>
              {!tradable && (
                <span className="text-[10px] text-slate-500 border border-white/10 rounded px-1 ml-1.5">ดูอย่างเดียว</span>
              )}
            </span>
            <span className={`font-bold ${scoreColor(o.score)}`}>{o.score.toFixed(0)}%</span>
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
        </div>
        );
      })}
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
