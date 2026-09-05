"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  BacktestConfig,
  BacktestResult,
  CorrelationResponse,
  EquityCurve,
  ExtendedAnalysis,
  FrequencyDecision,
  JournalAnalysis,
  KillSwitch,
  NewsRisk,
  PaperTrading,
  SessionStatus,
  SignalReport,
  WalkForwardResult,
} from "@/lib/types";

const INDICATORS = ["EMA", "RSI", "MACD", "ADX", "ATR", "SuperTrend", "PriceAction"] as const;
const ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"];

type Badge = "ok" | "warn" | "danger";

function StatusBadge({ label, status, text }: { label: string; status: Badge; text: string }) {
  const color =
    status === "ok" ? "text-profit border-profit/40" :
    status === "warn" ? "text-yellow-400 border-yellow-500/40" :
    "text-loss border-loss/40";
  return (
    <div className={`rounded border px-3 py-2 ${color}`}>
      <div className="text-xs uppercase tracking-wide opacity-70">{label}</div>
      <div className="font-semibold text-sm">{text}</div>
    </div>
  );
}

export default function PerformancePage() {
  const [freq, setFreq] = useState<FrequencyDecision | null>(null);
  const [news, setNews] = useState<NewsRisk | null>(null);
  const [session, setSession] = useState<SessionStatus | null>(null);
  const [corr, setCorr] = useState<CorrelationResponse | null>(null);
  const [kill, setKill] = useState<KillSwitch | null>(null);
  const [journal, setJournal] = useState<JournalAnalysis | null>(null);
  const [paper, setPaper] = useState<PaperTrading | null>(null);
  const [extended, setExtended] = useState<ExtendedAnalysis | null>(null);
  const [equity, setEquity] = useState<EquityCurve | null>(null);
  const [sigReport, setSigReport] = useState<SignalReport | null>(null);
  const [loading, setLoading] = useState(true);

  const [btAsset, setBtAsset] = useState<string>("EURUSD");
  const [btIndicator, setBtIndicator] = useState<(typeof INDICATORS)[number]>("EMA");
  const [btDays, setBtDays] = useState(120);
  const [btCapital, setBtCapital] = useState(10_000);
  const [btRiskPct, setBtRiskPct] = useState(1.0);
  const [bt, setBt] = useState<BacktestResult | null>(null);
  const [wf, setWf] = useState<WalkForwardResult | null>(null);
  const [btLoading, setBtLoading] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [f, n, s, c, k, j, p, x, eq, sr] = await Promise.all([
        api.tradingFrequency(), api.tradingCalendar(), api.tradingSession(),
        api.tradingCorrelation(), api.tradingKillSwitch(),
        api.tradingJournal(30), api.tradingPaper(), api.extendedAnalysis(),
        api.equityCurve(90), api.signalReport(30),
      ]);
      setFreq(f); setNews(n); setSession(s); setCorr(c);
      setKill(k); setJournal(j); setPaper(p); setExtended(x);
      setEquity(eq); setSigReport(sr);
    } catch {
      /* endpoints unreachable — badges stay null */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadAll(); }, [loadAll]);

  // seed backtest defaults from saved settings (capital = single source of truth)
  useEffect(() => {
    api.getSettings()
      .then((s) => {
        setBtAsset(s.backtest_asset);
        setBtIndicator(s.backtest_indicator as (typeof INDICATORS)[number]);
        setBtDays(s.backtest_days);
        setBtCapital(s.capital);
        setBtRiskPct(s.risk_per_trade_pct);
      })
      .catch(() => { /* backend unreachable — keep component defaults */ });
  }, []);

  const runBacktest = async () => {
    setBtLoading(true);
    setBt(null); setWf(null);
    const config: BacktestConfig = {
      asset: btAsset, indicator: btIndicator, days: btDays,
      initial_capital: btCapital, risk_per_trade_pct: btRiskPct,
    };
    try {
      const [b, w] = await Promise.all([
        api.tradingBacktest(config), api.tradingWalkForward(config),
      ]);
      setBt(b); setWf(w);
    } catch {
      /* ignore */
    } finally {
      setBtLoading(false);
    }
  };

  const newsBadge: Badge = news?.status === "SAFE" ? "ok" : news?.status === "CAUTION" ? "warn" : "danger";
  const killBadge: Badge = kill?.engaged ? "danger" : "ok";
  const freqBadge: Badge = freq?.allowed ? "ok" : "warn";

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Performance Dashboard</h1>
        <button onClick={loadAll} disabled={loading}
          className="text-sm bg-accent text-white font-semibold rounded px-3 py-2 min-h-[40px] disabled:opacity-50 active:brightness-90">
          {loading ? "กำลังโหลด..." : "รีเฟรช"}
        </button>
      </div>

      {/* Status grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatusBadge label="News Risk" status={newsBadge}
          text={news ? news.status : "—"} />
        <StatusBadge label="Session" status="ok"
          text={session ? (session.active_sessions.join(" + ") || "ปิดตลาด") : "—"} />
        <StatusBadge label="Correlation" status={corr && corr.portfolio_correlation > 80 ? "danger" : "ok"}
          text={corr ? `${corr.portfolio_correlation}/100` : "—"} />
        <StatusBadge label="Kill Switch" status={killBadge}
          text={kill ? (kill.engaged ? "ENGAGED" : "CLEAR") : "—"} />
        <StatusBadge label="Frequency" status={freqBadge}
          text={freq ? `${freq.trades_today}/${freq.limits?.max_trades_daily ?? "—"}` : "—"} />
        <StatusBadge label="Win Rate (30d)"
          status={journal && journal.total_trades >= 30 ? "ok" : "warn"}
          text={journal ? `${journal.win_rate_pct}% (n=${journal.total_trades})` : "—"} />
        <StatusBadge label="Profit Factor"
          status={journal && journal.total_trades >= 30 ? "ok" : "warn"}
          text={journal ? `${journal.profit_factor} (n=${journal.total_trades})` : "—"} />
        <StatusBadge label="Live Readiness" status={paper && paper.live_readiness_score >= 70 ? "ok" : "warn"}
          text={paper ? `${paper.live_readiness_score}/100` : "—"} />
      </div>

      {/* Detail panels */}
      <div className="grid md:grid-cols-2 gap-4">
        <div className="panel">
          <h2 className="panel-title">Economic Calendar / News Risk</h2>
          <p className="text-sm text-slate-300">{news?.reason ?? "ยังไม่มีข้อมูลปฏิทิน"}</p>
          <h3 className="text-sm font-semibold mt-3 text-slate-400">Exposure</h3>
          <ul className="text-sm space-y-1">
            {(corr?.exposure ?? []).map((e) => (
              <li key={e.currency} className="flex justify-between">
                <span>{e.currency}</span>
                <span className="text-slate-400">{e.exposure_pct}% ({e.direction_net})</span>
              </li>
            ))}
            {!corr?.exposure?.length && <li className="text-slate-500">ไม่มีโพซิชันเปิด</li>}
          </ul>
        </div>

        <div className="panel">
          <h2 className="panel-title">Journal Insight (30d)</h2>
          {journal ? (
            <div className="text-sm space-y-1">
              <div className="flex justify-between"><span>Trades</span><span>{journal.total_trades}</span></div>
              <div className="flex justify-between"><span>Win Rate</span><span className="text-profit">{journal.win_rate_pct}%</span></div>
              <div className="flex justify-between"><span>Profit Factor</span><span>{journal.profit_factor}</span></div>
              <div className="flex justify-between"><span>Average RR</span><span>{journal.average_rr}</span></div>
            </div>
          ) : <p className="text-sm text-slate-500">ยังไม่มีบันทึกเทรด</p>}
        </div>

        <div className="panel">
          <h2 className="panel-title">Paper Trading</h2>
          {paper ? (
            <div className="text-sm space-y-1">
              <div className="flex justify-between"><span>Virtual Capital</span><span>${paper.virtual_capital.toLocaleString()}</span></div>
              <div className="flex justify-between">
                <span>Virtual PnL</span>
                <span className={paper.virtual_pnl >= 0 ? "text-profit" : "text-loss"}>${paper.virtual_pnl}</span>
              </div>
              <div className="flex justify-between"><span>Open Orders</span><span>{paper.open_virtual_orders}</span></div>
              <p className="text-slate-400 pt-2">🤖 {paper.ai_coaching}</p>
            </div>
          ) : <p className="text-sm text-slate-500">—</p>}
        </div>

        <div className="panel">
          <h2 className="panel-title">Kill Switch</h2>
          <p className={`text-sm font-semibold ${kill?.engaged ? "text-loss" : "text-profit"}`}>
            {kill?.message ?? "—"}
          </p>
          {!!kill?.triggers.length && (
            <ul className="text-sm text-loss list-disc pl-4 pt-2">
              {kill.triggers.map((t) => <li key={t}>{t}</li>)}
            </ul>
          )}
        </div>
      </div>

      {/* Equity curve (equity_snapshots — one point per UTC day) */}
      <div className="panel">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h2 className="panel-title">Equity Curve</h2>
          {equity && (
            <div className="text-xs text-slate-400">
              Latest <span className="text-slate-200 font-semibold">${equity.latest_equity.toLocaleString()}</span>
              {" · "}Peak ${equity.peak_equity.toLocaleString()}
              {" · "}DD <span className={equity.drawdown_pct > 0 ? "text-loss" : "text-profit"}>
                {equity.drawdown_pct}%
              </span>
              {equity.synthetic && " · (ยังไม่มี snapshot — แสดงค่า capital)"}
            </div>
          )}
        </div>
        {equity && equity.points.length > 1 ? (
          <svg viewBox="0 0 600 180" className="w-full h-44 mt-2" preserveAspectRatio="none">
            {(() => {
              const pts = equity.points;
              const vals = pts.map((p) => p.equity);
              const min = Math.min(...vals, equity.capital);
              const max = Math.max(...vals, equity.capital);
              const span = max - min || 1;
              const x = (i: number) => (i / (pts.length - 1)) * 600;
              const y = (v: number) => 170 - ((v - min) / span) * 150;
              const path = pts.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
              const capY = y(equity.capital);
              const last = pts[pts.length - 1].equity;
              const up = last >= equity.capital;
              return (
                <>
                  <line x1="0" y1={capY} x2="600" y2={capY} stroke="#475569" strokeDasharray="4 4" strokeWidth="1" />
                  <path d={path} fill="none" stroke={up ? "#22c55e" : "#ef4444"} strokeWidth="2" />
                </>
              );
            })()}
          </svg>
        ) : (
          <p className="text-sm text-slate-500 mt-2">
            ยังไม่มีข้อมูล equity — ระบบจะบันทึก 1 จุด/วัน อัตโนมัติ (portfolio monitor worker)
          </p>
        )}
      </div>

      {/* Signal quality report (signal_logs ↔ paper_trades join) */}
      <div className="panel">
        <h2 className="panel-title">Signal Quality Report (30d)</h2>
        {sigReport && sigReport.matched_trades > 0 ? (
          <div className="grid md:grid-cols-3 gap-4 text-sm">
            {[
              { title: "ตามสินทรัพย์", rows: sigReport.by_asset },
              { title: "ตามช่วง Confidence", rows: sigReport.by_confidence_band },
              { title: "ตาม Regime", rows: sigReport.by_regime },
            ].map((g) => (
              <div key={g.title}>
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-2">{g.title}</p>
                <ul className="space-y-1">
                  {g.rows.map((r) => (
                    <li key={r.key} className="flex justify-between gap-2">
                      <span className="truncate">{r.key || "—"}</span>
                      <span className="text-slate-400 shrink-0">
                        {r.trades} ไม้ · <span className={r.total_pnl >= 0 ? "text-profit" : "text-loss"}>
                          {r.total_pnl >= 0 ? "+" : ""}{r.total_pnl.toFixed(2)}
                        </span>{" "}
                        · win {r.win_rate_pct}%
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-slate-500">
            ยังไม่มีสัญญาณที่ปิดแล้วให้วิเคราะห์ (ต้องมีไม้ที่เปิดจากสัญญาณและปิดแล้ว)
          </p>
        )}
      </div>

      {/* Backtest center */}
      <div className="panel">
        <h2 className="panel-title">Backtest Center + Walk Forward</h2>
        <div className="flex flex-wrap gap-3 items-end">
          <label className="text-sm">
            Asset
            <select value={btAsset} onChange={(e) => setBtAsset(e.target.value)}
              className="mt-1 block bg-surface border border-slate-700 rounded px-3 py-2">
              {ASSETS.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </label>
          <label className="text-sm">
            Indicator
            <select value={btIndicator} onChange={(e) => setBtIndicator(e.target.value as typeof btIndicator)}
              className="mt-1 block bg-surface border border-slate-700 rounded px-3 py-2">
              {INDICATORS.map((i) => <option key={i} value={i}>{i}</option>)}
            </select>
          </label>
          <label className="text-sm">
            Days
            <input type="number" min={30} max={365} value={btDays}
              onChange={(e) => setBtDays(Number(e.target.value) || 120)}
              className="mt-1 block w-24 bg-surface border border-slate-700 rounded px-3 py-2" />
          </label>
          <button onClick={runBacktest} disabled={btLoading}
            className="bg-accent text-white font-semibold rounded px-4 py-2.5 min-h-[44px] disabled:opacity-50 active:brightness-90">
            {btLoading ? "กำลังรัน..." : "รัน Backtest + Walk Forward"}
          </button>
        </div>

        {bt && (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3 pt-4 text-sm">
            <div><div className="text-slate-400 text-xs">Trades</div>{bt.total_trades}</div>
            <div><div className="text-slate-400 text-xs">Win Rate</div>{bt.win_rate_pct}%</div>
            <div><div className="text-slate-400 text-xs">Profit Factor</div>{bt.profit_factor}</div>
            <div><div className="text-slate-400 text-xs">Sharpe</div>{bt.sharpe_ratio}</div>
            <div><div className="text-slate-400 text-xs">Max DD</div>{bt.max_drawdown_pct}%</div>
          </div>
        )}
        {wf && (
          <div className="pt-3 text-sm">
            <div className="text-slate-400 text-xs">Walk Forward Reliability</div>
            <div className="flex items-center gap-3">
              <span className="text-lg font-bold">{wf.reliability_score}/100</span>
              <span className="text-slate-500 text-xs">
                {wf.segments} segments · IS {wf.in_sample_win_rates.map((v) => `${v}%`).join(", ")}
                {wf.out_sample_win_rates.length > 0 &&
                  ` · OOS ${wf.out_sample_win_rates.map((v) => `${v}%`).join(", ")}`}
              </span>
            </div>
            {wf.note && <p className="text-xs text-slate-500 pt-1">{wf.note}</p>}
          </div>
        )}
        {bt?.note && <p className="text-xs text-slate-500 pt-2">{bt.note}</p>}
      </div>

      {/* Extended output format */}
      {extended && (
        <div className="panel">
          <h2 className="panel-title">Extended Output Format</h2>
          <div className="text-sm space-y-2">
            <p><span className="text-slate-400">FINAL DECISION:</span> <span className="font-bold">{extended.final_decision}</span></p>
            {[
              ["NEWS & CALENDAR", extended.news_calendar],
              ["SESSION ANALYSIS", extended.session_analysis],
              ["CORRELATION ANALYSIS", extended.correlation_analysis],
              ["EXECUTION PLAN", extended.execution_plan],
              ["RISK OFFICER REVIEW", extended.risk_officer_review],
              ["JOURNAL INSIGHT", extended.journal_insight],
              ["BACKTEST RESULT", extended.backtest_result],
              ["PAPER TRADING STATUS", extended.paper_trading_status],
              ["KILL SWITCH STATUS", extended.kill_switch_status],
            ].map(([k, v]) => (
              <p key={k as string}><span className="text-slate-400">{k}:</span> {v}</p>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
