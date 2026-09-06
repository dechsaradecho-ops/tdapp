"use client";

import { useEffect, useState } from "react";
import RiskPanel from "@/components/RiskPanel";
import { api } from "@/lib/api";
import { usePortfolio } from "@/lib/portfolio";
import { RiskStatus } from "@/lib/types";

export default function RiskPage() {
  const { capital, equity, pnl, loaded } = usePortfolio();
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // รอให้ store โหลดค่าจริงจาก backend ก่อน — store เริ่มต้นที่ 0
    // และ endpoint บังคับ starting_capital/peak_equity > 0 (422 ถ้ายิง 0)
    if (!loaded || capital <= 0) return;
    api.checkRisk({
      starting_capital: capital,
      peak_equity: capital * 1.02,
      current_equity: equity,
      realized_pnl_today: Math.min(pnl, 0),
      realized_pnl_week: pnl,
      realized_pnl_month: pnl,
      open_risk: capital * 0.005,
    }).then(setRisk).catch((e) => setError(String(e)));
  }, [capital, equity, pnl, loaded]);

  return (
    <div className="max-w-xl space-y-4">
      <h2 className="panel-title">Risk Engine Status</h2>
      {error && <p className="text-loss text-sm">{error}</p>}
      <div className="panel">
        <RiskPanel risk={risk} />
      </div>
      <p className="text-xs text-slate-500">
        Limits: Risk/Trade 0.5% · Daily 2% · Weekly 5% · Monthly 8% · Max Drawdown 10%.
        เมื่อถึงขีดจำกัด ระบบจะ Pause Trading และแจ้งเตือนทันที
      </p>
    </div>
  );
}
