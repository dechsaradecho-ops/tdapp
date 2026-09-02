"use client";

import { useEffect, useState } from "react";
import PortfolioAllocation from "@/components/PortfolioAllocation";
import { api } from "@/lib/api";
import { fmtPct } from "@/lib/format";
import { PortfolioRecommendation } from "@/lib/types";

export default function SettingsPage() {
  const [capital, setCapital] = useState(100000);
  const [target, setTarget] = useState(3);
  const [profile, setProfile] = useState("moderate");
  const [maxDd, setMaxDd] = useState(10);
  const [rec, setRec] = useState<PortfolioRecommendation | null>(null);
  const [loading, setLoading] = useState(false);

  const recommend = async () => {
    setLoading(true);
    try {
      setRec(await api.recommendPortfolio({
        capital, target_return_pct: target, max_drawdown_pct: maxDd, risk_profile: profile,
      }));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { recommend(); /* eslint-disable-next-line */ }, []);

  return (
    <div className="grid md:grid-cols-2 gap-4">
      <div className="panel">
        <h2 className="panel-title">Portfolio Settings</h2>
        <div className="space-y-3">
          <NumField label="Capital (THB)" value={capital} onChange={setCapital} />
          <NumField label="Target Return (%/month)" value={target} onChange={setTarget} step={0.5} />
          <NumField label="Max Drawdown (%)" value={maxDd} onChange={setMaxDd} step={0.5} />
          <label className="block text-sm">
            Risk Profile
            <select value={profile} onChange={(e) => setProfile(e.target.value)}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2">
              <option value="conservative">Conservative</option>
              <option value="moderate">Moderate</option>
              <option value="aggressive">Aggressive</option>
            </select>
          </label>
          <button onClick={recommend} disabled={loading}
            className="w-full bg-accent text-surface font-semibold rounded py-2 disabled:opacity-50">
            {loading ? "กำลังคำนวณ..." : "ขอ Portfolio Recommendation"}
          </button>
        </div>
      </div>

      <div className="panel">
        <h2 className="panel-title">Portfolio Recommendation</h2>
        {rec ? (
          <div className="space-y-4">
            <PortfolioAllocation allocation={rec.allocation} />
            <div className="text-sm flex justify-between border-t border-slate-800 pt-3">
              <span className="text-slate-400">Expected Return</span>
              <span className="text-profit font-semibold">{fmtPct(rec.expected_monthly_return_pct, 2)}</span>
            </div>
            <div className="text-sm flex justify-between">
              <span className="text-slate-400">Expected Drawdown</span>
              <span className="text-amber-400 font-semibold">{fmtPct(rec.expected_drawdown_pct, 2)}</span>
            </div>
            <div>
              <p className="panel-title">เหตุผล</p>
              <ul className="list-disc list-inside text-sm space-y-1 text-slate-300">
                {rec.reasoning.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          </div>
        ) : (
          <p className="text-slate-500 text-sm">กำลังโหลด...</p>
        )}
      </div>
    </div>
  );
}

function NumField({ label, value, onChange, step = 1 }:
  { label: string; value: number; onChange: (v: number) => void; step?: number }) {
  return (
    <label className="block text-sm">
      {label}
      <input type="number" value={value} step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2" />
    </label>
  );
}
