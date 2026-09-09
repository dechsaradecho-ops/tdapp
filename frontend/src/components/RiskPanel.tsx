"use client";

import { RiskStatus } from "@/lib/types";
import { fmtNum } from "@/lib/format";
import Icon from "@/components/Icon";

function barColor(ratio: number): string {
  if (ratio > 0.8) return "bg-loss";
  if (ratio > 0.5) return "bg-amber-500";
  return "bg-profit";
}

function textColor(ratio: number): string {
  if (ratio > 0.8) return "text-loss font-bold";
  if (ratio > 0.5) return "text-amber-400 font-bold";
  return "text-slate-200";
}

/** แปล breach ภาษาอังกฤษจาก engine เป็นภาษาไทยอ่านง่าย */
function thaiBreach(b: string): string {
  if (b.startsWith("Daily loss")) return `ขาดทุนวันนี้ ${b.slice(11)}`;
  if (b.startsWith("Weekly loss")) return `ขาดทุน 7 วัน ${b.slice(12)}`;
  if (b.startsWith("Monthly loss")) return `ขาดทุน 30 วัน ${b.slice(13)}`;
  if (b.startsWith("Drawdown")) return `Drawdown ${b.slice(9)}`;
  if (b.startsWith("Open risk")) return `ความเสี่ยงไม้เปิด ${b.slice(10)}`;
  return b;
}

function LimitRow({ label, value, limit, suffix = "%" }: {
  label: string; value: number; limit: number; suffix?: string;
}) {
  const ratio = limit > 0 ? value / limit : 0;
  const pct = Math.min(100, ratio * 100);
  return (
    <div className="bg-white/[0.05] rounded-xl p-2 border border-white/10">
      <div className="flex justify-between items-baseline">
        <p className="text-xs text-slate-500">{label}</p>
        <p className={`text-sm font-semibold tabular-nums ${textColor(ratio)}`}>
          {fmtNum(value, 2)}{suffix}
          {limit > 0 && (
            <span className="text-xs font-normal text-slate-500"> / {fmtNum(limit, 2)}{suffix}</span>
          )}
        </p>
      </div>
      {limit > 0 && (
        <div className="h-1.5 bg-slate-800 rounded overflow-hidden mt-1.5">
          <div className={`h-full ${barColor(ratio)}`} style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
}

export default function RiskPanel({ risk }: { risk: RiskStatus | null }) {
  if (!risk) return <p className="text-slate-500 text-sm">ยังไม่มีข้อมูลความเสี่ยง</p>;

  const ddRatio = risk.max_drawdown_pct > 0
    ? risk.current_drawdown_pct / risk.max_drawdown_pct : 0;
  const ddPct = Math.min(100, ddRatio * 100);
  const breaches = risk.breaches?.length ? risk.breaches : (risk.trading_paused ? [risk.message] : []);
  // Headroom งบ daily ที่เหลือสำหรับไม้ใหม่ (ติดลบ = เกินงบแล้ว)
  const dailyHeadroom = (risk.daily_loss_limit || 0) - (risk.open_risk_pct || 0);
  const newTradeRisk = risk.risk_per_trade_pct || 0;

  return (
    <div className="space-y-3 text-sm">
      {risk.trading_paused && (
        <div className="border border-loss bg-loss/10 rounded-xl p-3">
          <p className="font-bold text-loss flex items-center gap-1.5">
            <Icon n="ban" size={15} /> หยุดเทรด — ต้องตรวจสอบก่อน
          </p>
          {breaches.map((b, i) => (
            <p key={i} className="text-xs text-slate-300 mt-1">• {thaiBreach(b)}</p>
          ))}
        </div>
      )}
      <div className="flex justify-between items-center">
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
          <span className="tabular-nums">
            {fmtNum(risk.current_drawdown_pct, 2)}% / {fmtNum(risk.max_drawdown_pct, 2)}%
          </span>
        </div>
        <div className="h-2 bg-slate-800 rounded overflow-hidden">
          <div className={`h-full ${barColor(ddRatio)}`} style={{ width: `${ddPct}%` }} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <LimitRow label="ขาดทุนวันนี้" value={risk.daily_loss_pct} limit={risk.daily_loss_limit} />
        <LimitRow label="ขาดทุน 7 วัน" value={risk.weekly_loss_pct} limit={risk.weekly_loss_limit} />
        <LimitRow label="ขาดทุน 30 วัน" value={risk.monthly_loss_pct} limit={risk.monthly_loss_limit} />
        <LimitRow label="Open Risk (ถ้าโดน SL ทุกไม้)" value={risk.open_risk_pct} limit={risk.daily_loss_limit} />
      </div>
      {/* ที่มาของ Open Risk + headroom ไม้ใหม่ */}
      <div className="bg-white/[0.05] rounded-xl p-2 border border-white/10 text-xs text-slate-400 space-y-1">
        <p>
          Open Risk ${fmtNum(risk.open_risk_amount || 0, 2)}
          {risk.open_positions > 0 && ` จาก ${risk.open_positions} ไม้เปิด`}
          {risk.equity > 0 && ` (equity $${fmtNum(risk.equity, 2)})`}
        </p>
        {risk.daily_loss_limit > 0 && (
          <p className={dailyHeadroom >= newTradeRisk ? "" : "text-loss font-semibold"}>
            งบ daily เหลือ {fmtNum(dailyHeadroom, 2)}%
            {newTradeRisk > 0 && ` — ไม้ใหม่ใช้ ${fmtNum(newTradeRisk, 2)}% → ${
              dailyHeadroom >= newTradeRisk ? "เปิดได้" : "เปิดไม่ได้ (เกินงบ)"
            }`}
          </p>
        )}
      </div>
      {!risk.trading_paused && (
        <p className="text-xs text-profit">✓ ทุกค่าอยู่ในลิมิต — เทรดได้ปกติ</p>
      )}
    </div>
  );
}
