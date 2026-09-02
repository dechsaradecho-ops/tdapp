"use client";

import { useEffect, useState } from "react";
import RiskPanel from "@/components/RiskPanel";
import { api } from "@/lib/api";
import { RiskStatus } from "@/lib/types";

export default function RiskPage() {
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.checkRisk({
      starting_capital: 100000,
      peak_equity: 102000,
      current_equity: 101200,
      realized_pnl_today: -120,
      realized_pnl_week: 300,
      realized_pnl_month: 1200,
      open_risk: 500,
    }).then(setRisk).catch((e) => setError(String(e)));
  }, []);

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
