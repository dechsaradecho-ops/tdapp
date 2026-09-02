"use client";

import { useEffect, useState } from "react";
import GoalForm from "@/components/GoalForm";
import OpportunityScore from "@/components/OpportunityScore";
import TradingViewChart from "@/components/TradingViewChart";
import { api } from "@/lib/api";
import { fmtMoney } from "@/lib/format";
import { MarketSummary } from "@/lib/types";

export default function DashboardPage() {
  const [summary, setSummary] = useState<MarketSummary | null>(null);

  useEffect(() => {
    api.marketSummary().then(setSummary).catch(() => setSummary(null));
  }, []);

  return (
    <div className="space-y-6">
      <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Stat label="Capital" value={fmtMoney(100000)} />
        <Stat label="Current Equity" value={fmtMoney(101200)} positive />
        <Stat label="Current PnL" value={fmtMoney(1200)} positive />
        <Stat label="Monthly Goal" value="3%" />
      </section>

      <GoalForm />

      <section className="grid md:grid-cols-3 gap-4">
        <div className="panel md:col-span-2">
          <h2 className="panel-title">XAUUSD — TradingView</h2>
          <TradingViewChart symbol="OANDA:XAUUSD" />
        </div>
        <div className="panel">
          <h2 className="panel-title">Opportunity Score</h2>
          <OpportunityScore opportunities={summary?.opportunities ?? []} />
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, positive }: { label: string; value: string; positive?: boolean }) {
  return (
    <div className="panel">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`text-xl font-bold ${positive ? "text-profit" : ""}`}>{value}</p>
    </div>
  );
}
