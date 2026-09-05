"use client";

import { RiskStatus } from "@/lib/types";

export default function RiskPanel({ risk }: { risk: RiskStatus | null }) {
  if (!risk) return <p className="text-slate-500 text-sm">ยังไม่มีข้อมูลความเสี่ยง</p>;

  const ddPct = Math.min(100, (risk.current_drawdown_pct / risk.max_drawdown_pct) * 100);

  return (
    <div className="space-y-3 text-sm">
      {risk.trading_paused && (
        <div className="border border-loss bg-loss/10 rounded p-3 font-semibold">
          ⛔ {risk.message}
        </div>
      )}
      <div className="flex justify-between">
        <span className="text-slate-400">Risk Level</span>
        <span className={
          risk.risk_level === "critical" ? "text-loss font-bold"
          : risk.risk_level === "high" ? "text-amber-400 font-bold"
          : risk.risk_level === "medium" ? "text-yellow-300" : "text-profit"
        }>{risk.risk_level.toUpperCase()}</span>
      </div>
      <div>
        <div className="flex justify-between mb-1">
          <span className="text-slate-400">Drawdown</span>
          <span>{risk.current_drawdown_pct.toFixed(2)}% / {risk.max_drawdown_pct.toFixed(2)}%</span>
        </div>
        <div className="h-2 bg-slate-800 rounded overflow-hidden">
          <div className={`h-full ${ddPct > 80 ? "bg-loss" : ddPct > 50 ? "bg-amber-500" : "bg-profit"}`}
            style={{ width: `${ddPct}%` }} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Metric label="Daily Loss" value={`${risk.daily_loss_pct.toFixed(2)}%`} />
        <Metric label="Weekly Loss" value={`${risk.weekly_loss_pct.toFixed(2)}%`} />
        <Metric label="Monthly Loss" value={`${risk.monthly_loss_pct.toFixed(2)}%`} />
        <Metric label="Open Risk" value={`${risk.open_risk_pct.toFixed(2)}%`} />
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-white/[0.05] rounded-xl p-2 border border-white/10">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="font-semibold">{value}</p>
    </div>
  );
}
