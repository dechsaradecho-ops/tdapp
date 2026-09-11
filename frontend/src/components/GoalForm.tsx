"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import GlassSelect from "@/components/GlassSelect";
import Icon from "@/components/Icon";
import { fmtMoney, probabilityLabel } from "@/lib/format";
import { usePortfolio } from "@/lib/portfolio";
import { GoalAssessment, GoalRealityContext } from "@/lib/types";

const SCENARIO_LABELS: Record<string, string> = {
  best_case: "Best Case",
  normal_case: "Normal Case",
  worst_case: "Worst Case",
};

const RISK_PROFILES = [
  { value: "conservative", label: "Conservative" },
  { value: "moderate", label: "Moderate" },
  { value: "aggressive", label: "Aggressive" },
];

const REGIME_LABELS: Record<string, string> = {
  strong_bull_trend: "Bull แรง",
  bull_trend: "Bull Trend",
  sideway: "Sideway",
  high_volatility: "ผันผวนสูง",
  bear_trend: "Bear Trend",
  strong_bear_trend: "Bear แรง",
  news_driven_market: "ขับเคลื่อนด้วยข่าว",
};

export default function GoalForm({ onAssessed }: { onAssessed?: (targetPct: number) => void }) {
  const { capital, setCapital } = usePortfolio();
  const [target, setTarget] = useState(3);
  const [profile, setProfile] = useState("moderate");
  const [maxDd, setMaxDd] = useState(10);
  const [mode, setMode] = useState("manual");
  const [result, setResult] = useState<GoalAssessment | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [seeded, setSeeded] = useState(false);

  // Seed from the single source of truth — trading_settings (same values
  // the chat context + execution gate use) + last assessed target.
  // Fail-soft: settings may 401 before PIN login; keep the hardcoded
  // defaults so the form stays usable offline.
  useEffect(() => {
    let alive = true;
    try {
      const saved = window.localStorage.getItem("tdapp_goal_target");
      if (saved != null && Number.isFinite(Number(saved))) {
        const v = Math.min(100, Math.max(0.5, Number(saved)));
        if (alive) setTarget(v);
      }
    } catch { /* private mode — ignore */ }
    api.getSettings()
      .then((s) => {
        if (!alive) return;
        if (s.risk_profile) setProfile(s.risk_profile);
        if (Number.isFinite(s.max_drawdown_pct) && s.max_drawdown_pct > 0) setMaxDd(s.max_drawdown_pct);
        if (s.order_mode) setMode(s.order_mode);
        // Capital lives in the portfolio store (monitor snapshot); the
        // store's own CapitalSync seeds it — don't fight it here.
        setSeeded(true);
      })
      .catch(() => { if (alive) setSeeded(true); });
    return () => { alive = false; };
  }, []);

  const submit = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.assessGoal({
        capital, target_return_pct: target,
        risk_profile: profile, max_drawdown_pct: maxDd, trading_mode: mode,
      });
      setResult(r);
      try { window.localStorage.setItem("tdapp_goal_target", String(target)); } catch { /* ignore */ }
      onAssessed?.(target);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  const probColor = useMemo(() => {
    switch (result?.probability) {
      case "high_probability": return "text-profit";
      case "moderate_probability": return "text-amber-400";
      case "low_probability": return "text-loss";
      default: return "";
    }
  }, [result]);

  return (
    <div className="grid md:grid-cols-2 gap-4">
      <div className="panel">
        <h2 className="panel-title">Goal Engine — ประเมินความเป็นไปได้ของเป้าหมาย</h2>
        {!seeded && (
          <p className="text-xs text-slate-500 mb-2">กำลังโหลดค่าจากตั้งค่า…</p>
        )}
        <div className="space-y-3">
          <label className="block text-sm">
            Capital (USD)
            <input type="number" value={capital} min={1}
              onChange={(e) => setCapital(Number(e.target.value))}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2" />
          </label>
          <label className="block text-sm">
            Target Return (% monthly)
            <input type="number" value={target} step={0.5} min={0.5} max={100}
              onChange={(e) => setTarget(Number(e.target.value))}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2" />
          </label>
          <label className="block text-sm">
            Risk Profile
            <GlassSelect value={profile} onChange={setProfile} className="mt-1 w-full"
              options={RISK_PROFILES} />
          </label>
          <label className="block text-sm">
            Max Drawdown (%)
            <input type="number" value={maxDd} step={0.5} min={1} max={100}
              onChange={(e) => setMaxDd(Number(e.target.value))}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2" />
          </label>
          <label className="block text-sm">
            Trading Mode
            <GlassSelect value={mode} onChange={setMode} className="mt-1 w-full"
              options={[
                { value: "auto", label: "AUTO" },
                { value: "semi_auto", label: "SEMI-AUTO" },
                { value: "manual", label: "MANUAL" },
              ]} />
          </label>
          <button onClick={submit} disabled={loading}
            aria-busy={loading}
            className="w-full bg-accent text-white font-semibold rounded py-2 hover:brightness-110 disabled:opacity-50">
            <span className="inline-flex items-center justify-center gap-1.5">
              {loading && <Icon n="spinner" size={15} className="animate-spin" />}
              {loading ? "กำลังประเมิน..." : "ประเมินเป้าหมาย"}
            </span>
          </button>
          {error && <p className="text-loss text-sm">{error}</p>}
        </div>
      </div>

      <div className="panel">
        <h2 className="panel-title">ผลการประเมิน</h2>
        {!result && <p className="text-slate-500 text-sm">กรอกข้อมูลแล้วกดปุ่มเพื่อประเมิน</p>}
        {result && (
          <div className="space-y-4">
            {result.reality && <RealityPanel reality={result.reality} />}
            <div>
              <p className="text-sm text-slate-400">Expected Profit</p>
              <p className="text-2xl font-bold">{fmtMoney(result.expected_profit)}</p>
            </div>
            <div>
              <p className="text-sm text-slate-400">Probability</p>
              <p className={`text-xl font-bold ${probColor}`}>{probabilityLabel(result.probability)}</p>
            </div>
            {result.risk_warning && (
              <div className="border border-loss/40 bg-loss/10 rounded p-3 text-sm flex items-start gap-1.5">
                <Icon n="warning" size={14} className="mt-0.5 text-loss" />
                <span>{result.risk_warning}</span>
              </div>
            )}
            <div className="space-y-2">
              {result.scenarios.map((s) => (
                <div key={s.label} className="flex justify-between text-sm border-b border-slate-800 pb-1">
                  <span>{SCENARIO_LABELS[s.label]}</span>
                  <span className={s.expected_profit >= 0 ? "text-profit" : "text-loss"}>
                    {s.expected_return_pct.toFixed(1)}% ({fmtMoney(s.expected_profit)})
                  </span>
                </div>
              ))}
            </div>
            <div>
              <p className="panel-title">AI Reasoning</p>
              <ol className="list-decimal list-inside text-sm space-y-1 text-slate-300">
                {result.reasoning.map((r, i) => <li key={i}>{r}</li>)}
              </ol>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** Live trading state the assessment was adjusted by — "ประเมินจากสถานะจริงของคุณ". */
function RealityPanel({ reality }: { reality: GoalRealityContext }) {
  if (!reality.data_available) {
    return (
      <div className="border border-white/10 bg-white/[0.03] rounded-xl p-3 text-sm text-slate-400">
        ยังไม่มีข้อมูลการเทรดจริง — ผลนี้คำนวณจากสูตรทฤษฎีล้วน
        (เริ่มเทรดแล้วระบบจะปรับผลตาม PnL / Win Rate / ตลาดจริงให้อัตโนมัติ)
      </div>
    );
  }
  const regime = REGIME_LABELS[reality.market_regime] ?? reality.market_regime;
  const pnlColor = reality.pnl_total > 0 ? "text-profit" : reality.pnl_total < 0 ? "text-loss" : "";
  const blocked = reality.kill_switch_engaged || reality.trading_paused;
  const unreal = reality.unrealized_pnl ?? 0;
  const unrealColor = unreal > 0 ? "text-profit" : unreal < 0 ? "text-loss" : "";
  const equity = reality.equity ?? 0;
  const dd = reality.drawdown_pct ?? 0;
  return (
    <div className={`border rounded p-3 text-sm space-y-2 ${blocked ? "border-loss/40 bg-loss/10" : "border-accent/30 bg-accent/5"}`}>
      <p className="font-semibold text-slate-200 flex items-center gap-1.5"><Icon n="target" size={14} /> ประเมินจากสถานะจริงของคุณ</p>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-slate-300">
        <span>PnL รวม (ปิดแล้ว)</span>
        <span className={`text-right font-semibold ${pnlColor}`}>
          {reality.pnl_total >= 0 ? "+" : ""}{fmtMoney(reality.pnl_total)}
        </span>
        {unreal !== 0 && (
          <>
            <span>ไม้ค้าง (Unrealized)</span>
            <span className={`text-right font-semibold ${unrealColor}`}>
              {unreal >= 0 ? "+" : ""}{fmtMoney(unreal)}
            </span>
          </>
        )}
        {equity > 0 && (
          <>
            <span>Equity ปัจจุบัน</span>
            <span className="text-right font-semibold">{fmtMoney(equity)}</span>
          </>
        )}
        {dd > 0 && (
          <>
            <span>Drawdown (peak→ตอนนี้)</span>
            <span className="text-right font-semibold">{dd.toFixed(1)}%</span>
          </>
        )}
        <span>Win Rate</span>
        <span className="text-right font-semibold">{reality.win_rate.toFixed(0)}% ({reality.closed_count} ไม้)</span>
        <span>ไม้เปิดค้าง</span>
        <span className="text-right font-semibold">{reality.open_positions}</span>
        {(reality.trades_today != null || reality.trades_week != null) && (
          <>
            <span>ความถี่ (วัน/สัปดาห์)</span>
            <span className="text-right font-semibold">{reality.trades_today ?? 0} / {reality.trades_week ?? 0}</span>
          </>
        )}
        <span>ตลาดตอนนี้</span>
        <span className="text-right font-semibold">
          {regime}{reality.top_asset ? ` · ${reality.top_asset}` : ""}{reality.top_score ? ` ${reality.top_score.toFixed(0)}%` : ""}
        </span>
        {reality.order_mode && (
          <>
            <span>โหมดเทรด</span>
            <span className="text-right font-semibold">{reality.order_mode.toUpperCase()}</span>
          </>
        )}
        {reality.risk_per_trade_pct ? (
          <>
            <span>Risk / ไม้</span>
            <span className="text-right font-semibold">{reality.risk_per_trade_pct}%</span>
          </>
        ) : null}
      </div>
      {reality.kill_switch_engaged && (
        <p className="text-loss flex items-start gap-1.5"><Icon n="octagon" size={14} className="mt-0.5" /><span>Kill Switch: {reality.kill_triggers.join("; ")}</span></p>
      )}
      {reality.trading_paused && (
        <p className="text-amber-400 flex items-start gap-1.5"><Icon n="pause" size={14} className="mt-0.5" /><span>หยุดเทรดด้วยตนเอง{reality.pause_reason ? ` — ${reality.pause_reason}` : ""}</span></p>
      )}
    </div>
  );
}
