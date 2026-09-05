"use client";

import { scoreColor } from "@/lib/format";
import { AssetOpportunity } from "@/lib/types";

const BAND_LABEL: Record<string, string> = {
  very_high: "Very High",
  high: "High",
  medium: "Medium",
  low: "Low",
};

export default function OpportunityScore({ opportunities, loading, error }: {
  opportunities: AssetOpportunity[];
  loading?: boolean;
  error?: boolean;
}) {
  if (loading) {
    return <p className="text-slate-500 text-sm animate-pulse">⏳ กำลังโหลดข้อมูลตลาด... (Render cold start อาจใช้เวลาสักครู่)</p>;
  }
  if (!opportunities.length) {
    return error
      ? <p className="text-amber-400 text-sm">โหลดข้อมูลไม่สำเร็จ — รีเฟรชหน้าเพื่อลองอีกครั้ง</p>
      : <p className="text-slate-500 text-sm">ยังไม่มีข้อมูล — รอ Market Scanner ทำงานก่อน</p>;
  }
  return (
    <div className="space-y-3">
      {opportunities.map((o) => (
        <div key={o.asset} className="border-b border-slate-800 pb-2 last:border-0">
          <div className="flex items-center justify-between">
            <span className="font-semibold">{o.asset}</span>
            <span className={`font-bold ${scoreColor(o.score)}`}>{o.score.toFixed(0)}</span>
          </div>
          <div className="h-2 bg-slate-800 rounded mt-1 overflow-hidden">
            <div
              className={`h-full rounded ${o.score >= 81 ? "bg-emerald-500" : o.score >= 61 ? "bg-accent" : o.score >= 31 ? "bg-amber-500" : "bg-slate-600"}`}
              style={{ width: `${o.score}%` }}
            />
          </div>
          <div className="flex justify-between text-xs text-slate-500 mt-1">
            <span>{BAND_LABEL[o.band] ?? o.band} Opportunity</span>
            <span>{o.reasons[0]?.slice(0, 60) ?? ""}</span>
          </div>
        </div>
      ))}
    </div>
  );
}
