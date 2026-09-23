"use client";

import { useEffect, useState } from "react";
import AutoTradeReadinessCard from "@/components/AutoTradeReadinessCard";
import GoalForm from "@/components/GoalForm";
import GlassSelect from "@/components/GlassSelect";
import LoadingGraphic from "@/components/LoadingGraphic";
import MarketClosedBanner from "@/components/MarketClosedBanner";
import OpportunityScore from "@/components/OpportunityScore";
import TradingViewChart from "@/components/TradingViewChart";
import { GIT_SHA } from "@/lib/gitVersion";
import { api } from "@/lib/api";
import { fmtMoney, scoreColor } from "@/lib/format";
import { usePortfolio } from "@/lib/portfolio";
import { AppSettings, MarketSummary, PnlBreakdown, PnlBreakdownRow } from "@/lib/types";

// เหตุผลการปิดไม้ → ป้ายไทยที่อ่านง่ายบนหน้าหลัก (ค่า key = close_reason
// ดิบจาก backend: "sl" / "tp" / "smart_exit:left_behind" / "manual" / ...)
const CLOSE_REASON_LABELS: Record<string, string> = {
  sl: "ตัดขาดทุน (SL)",
  tp: "ถึงเป้า (TP)",
  "smart_exit:left_behind": "Smart Exit · ไม้ตกขบวน",
  "smart_exit:trailing": "Smart Exit · Trailing",
  "smart_exit:reversal": "Smart Exit · กลับทิศ",
  "smart_exit:time": "Smart Exit · หมดเวลา",
  manual: "ปิดมือ",
  manual_half: "ปิดมือ · ครึ่งไม้",
  time: "หมดเวลา (Time Stop)",
  emergency: "ปิดฉุกเฉิน",
  "kill_expand": "Kill Expand",
  close_group: "ปิดทั้งกลุ่ม",
};
const reasonLabel = (k: string) => {
  const key = (k || "—").trim();
  if (CLOSE_REASON_LABELS[key]) return CLOSE_REASON_LABELS[key];
  // smart_exit:xxx ที่ยังไม่รู้จัก → แปลง _ เป็นเว้นวรรคให้อ่านออก
  if (key.startsWith("smart_exit:")) {
    return `Smart Exit · ${key.slice("smart_exit:".length).replace(/_/g, " ")}`;
  }
  return key;
};

// เดิมอยู่หน้า /market (รวมเข้าหน้าหลักตามแผนจัดเมนูใหม่ Plan B)
const SYMBOLS: Record<string, string> = {
  XAUUSD: "OANDA:XAUUSD",
  EURUSD: "OANDA:EURUSD",
  USDJPY: "OANDA:USDJPY",
  GBPUSD: "OANDA:GBPUSD",
  AUDUSD: "OANDA:AUDUSD",
};
// Pairs ที่ไม่มีใน SYMBOLS (เช่น CHFJPY จาก allowed_assets) ใช้ generic OANDA mapping
const tvSymbol = (a: string) => SYMBOLS[a] ?? `OANDA:${a}`;

export default function DashboardPage() {
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryErr, setSummaryErr] = useState(false);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [selected, setSelected] = useState("XAUUSD");
  // Monthly Goal stat — reflects the last assessed target (persisted by
  // GoalForm in localStorage), not a hardcoded 3%.
  const [goalPct, setGoalPct] = useState(3);
  // Market clock — ตลาดปิดสุดสัปดาห์ → ขึ้นแบนเนอร์ "ตลาดปิด" ด้านบนสุด
  // (backend บังคับ Gate 0b ห้ามเปิดออเดอร์ใหม่; นี่คือหน้าตาของกฎนั้น)
  const [marketClosed, setMarketClosed] = useState(false);
  const [nextOpenUtc, setNextOpenUtc] = useState<string | null>(null);
  // สถิติแยกตามเหตุผลการปิด/สินทรัพย์ (จาก /pnl-breakdown, days=0 = ทั้งหมด)
  const [breakdown, setBreakdown] = useState<PnlBreakdown | null>(null);
  const { capital, equity, pnl } = usePortfolio();

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem("tdapp_goal_target");
      if (saved != null && Number.isFinite(Number(saved))) {
        setGoalPct(Math.min(100, Math.max(0.5, Number(saved))));
      }
    } catch { /* private mode — keep default */ }
    api.marketSummary()
      .then((s) => { setSummary(s); setSummaryLoading(false); })
      .catch(() => { setSummaryErr(true); setSummaryLoading(false); });
    // allowed_assets (trading whitelist) — ใช้แยก badge "ดูอย่างเดียว" ให้
    // สัญลักษณ์ที่ประเมินแต่ไม่เข้าระบบสัญญาณ/เทรด; ล้มเหลวได้ (auth) — หน้ายังใช้ได้
    api.getSettings().then(setSettings).catch(() => { });
    // Market clock สำหรับแบนเนอร์ "ตลาดปิด" — ล้มเหลวได้ (auth) หน้าไม่พัง
    api.monitor()
      .then((m) => {
        setMarketClosed(Boolean(m.market_closed));
        setNextOpenUtc(m.next_open_utc ?? null);
      })
      .catch(() => { });
    // สถิติ realized PnL แยกตามเหตุผลการปิด + สินทรัพย์ (days=0 = ทั้งหมด)
    api.pnlBreakdown(0).then(setBreakdown).catch(() => { });
  }, []);

  // ปุ่มสัญลักษณ์ = ทุก asset ใน summary.opportunities (follows allowed_assets)
  // fallback เป็น SYMBOLS เดิมถ้ายังไม่มีข้อมูล
  const symbolList = summary?.opportunities.length
    ? summary.opportunities.map((o) => o.asset)
    : Object.keys(SYMBOLS);
  const confByAsset = new Map(
    (summary?.opportunities ?? []).map((o) => [o.asset, o.score]),
  );
  // dropdown options — เรียง confidence มาก → น้อย (fallback ที่ยังไม่มีคะแนน
  // ไปอยู่ท้ายสุด), badge % สีตามเกณฑ์เดียวกับ OpportunityScore
  const tradableSet = settings?.allowed_assets?.length
    ? new Set(settings.allowed_assets) : null;
  const symbolOptions = symbolList
    .map((a) => {
      const conf = confByAsset.get(a);
      const tradable = !tradableSet || tradableSet.has(a);
      return {
        value: a,
        plainLabel: conf != null ? `${a} · ${conf.toFixed(0)}%` : a,
        label: (
          <span className="inline-flex items-center gap-1.5">
            {a}
            {conf != null && (
              <span className={`text-xs font-bold ${scoreColor(conf)}`}>
                {conf.toFixed(0)}%
              </span>
            )}
            {!tradable && (
              <span className="text-[10px] text-slate-500 border border-white/10 rounded px-1">ดูอย่างเดียว</span>
            )}
          </span>
        ),
      };
    })
    .sort((x, y) => {
      const cx = confByAsset.get(x.value) ?? -1;
      const cy = confByAsset.get(y.value) ?? -1;
      return cy - cx;
    });

  return (
    <>
      {/* แบ็กกราวด์ฮีโร่ (GSAP ScrollTrigger image zoom) mount ที่
          app/layout.tsx ผ่าน <AppHero /> — แสดงทุกหน้า และแถบเริ่มที่ขอบบนสุด
          ของหน้า ตรงกับ header (ไม่ติด padding ของ <main>) */}

      {/* เนื้อหาทั้งหน้า — ยกขึ้นชั้นบน (z-10) ให้ลอยเหนือแบ็กกราวด์
          (ตอนนี้ .zoom-hero มี z-index: -1 จึงไม่จำเป็นแล้ว แต่คงไว้เพื่อความชัดเจน) */}
      <div className="relative z-10 space-y-6">
        {/* ---------- ตลาดปิด: ห้ามเปิดออเดอร์ใหม่ (Gate 0b) ---------- */}
        <MarketClosedBanner marketClosed={marketClosed} nextOpenUtc={nextOpenUtc} />

        <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Stat label="Capital" value={fmtMoney(capital)} />
          <Stat label="Current Equity" value={fmtMoney(equity)} positive={pnl >= 0} />
          <Stat label="Current PnL" value={fmtMoney(pnl)} positive={pnl >= 0} />
          <Stat label="Monthly Goal" value={`${goalPct}%`} />
        </section>

        {/* ---------- สถานะการเทรดอัตโนมัติ: เปิดได้/ไม่ได้ เพราะปัจจัยอะไร ---------- */}
        <AutoTradeReadinessCard />

        <GoalForm onAssessed={setGoalPct} />

        {/* ---------- Market Regime Analysis (จาก /market เดิม) ---------- */}
        <section className="panel">
          <h2 className="panel-title">Market Regime Analysis</h2>
          {summary ? (
            <div className="grid md:grid-cols-3 gap-4">
              <div>
                <p className="text-xs text-slate-500">Regime</p>
                <p className="text-xl font-bold">{summary.regime.replace(/_/g, " ").toUpperCase()}</p>
              </div>
              <div>
                <p className="text-xs text-slate-500">Confidence</p>
                <p className="text-xl font-bold text-accent">{summary.confidence}%</p>
              </div>
              <div>
                <p className="text-xs text-slate-500">Sentiment</p>
                <p className={`text-xl font-bold ${summary.sentiment === "bullish" ? "text-profit" : summary.sentiment === "bearish" ? "text-loss" : ""}`}>
                  {summary.sentiment.toUpperCase()}
                </p>
              </div>
              <p className="md:col-span-3 text-sm text-slate-400">{summary.explanation}</p>
            </div>
          ) : summaryLoading ? (
            <LoadingGraphic message="กำลังโหลดข้อมูลตลาด..." compact />
          ) : (
            <p className="text-slate-500 text-sm">
              ไม่มีข้อมูล — ตรวจสอบว่า backend รันอยู่
            </p>
          )}
        </section>

        <section className="grid md:grid-cols-3 gap-4">
          <div className="panel md:col-span-2">
            {/* ตัวเลือกสัญลักษณ์ (จาก /market เดิม) — XAUUSD ค่าเริ่มต้น
                เดิมเป็น chip 28 ปุ่มเล็มพื้นที่หน้าจอมาก (ผู้ใช้ขอ 2026-09-07) →
                dropdown เดียว + เรียงตาม confidence มาก → น้อย
                GlassSelect รับ label เป็น JSX ได้แล้ว (badge % สีตามเกณฑ์) */}
            <div className="mb-3">
              <GlassSelect
                value={selected}
                onChange={setSelected}
                className="w-full md:max-w-xs"
                ariaLabel="เลือกสัญลักษณ์"
                options={symbolOptions}
              />
            </div>
            <TradingViewChart symbol={tvSymbol(selected)} />
          </div>
          <div className="panel">
            <h2 className="panel-title">Opportunity Score &amp; Confidence Score</h2>
            <OpportunityScore
              opportunities={summary?.opportunities ?? []}
              loading={summaryLoading}
              error={summaryErr}
              minConfidence={summary?.min_confidence}
              minConfidenceGold={summary?.min_confidence_gold}
              tradableAssets={settings?.allowed_assets}
            />
          </div>
        </section>
        {/* ---------- สถิติแยกตามเหตุผลการปิด + สินทรัพย์ ---------- */}
        <PnlBreakdownPanels data={breakdown} />

        {/* เลขเวอร์ชัน git ตอน build — ตัวเล็กจาง ๆ ล่างสุดของโฮม */}
        <p className="pt-2 pb-1 text-center text-[10px] leading-none text-slate-600/70 select-none" aria-hidden="true">
          v{GIT_SHA}
        </p>
      </div>
    </>
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

/** 2 การ์ดสถิติ: realized PnL แยกตามเหตุผลการปิด และแยกตามสินทรัพย์.
 *
 * อ้างอิงรูปแบบจาก PerformancePanel (key · n ไม้ · ±PnL · win %) และ
 * เพิ่มสัดส่วนชนะ/แพ้ (W/L) ให้ทุกแถว — ขาดทุน −$19.30 จาก 6 ไม้อ่าน
 * ต่างกันมากระหว่าง 3W/3L กับ 0W/6L. แถวบนสุด (แย่สุด) ตัวหนา. */
function PnlBreakdownPanels({ data }: { data: PnlBreakdown | null }) {
  return (
    <section className="grid md:grid-cols-2 gap-4">
      <BreakdownCard
        title="แยกตามเหตุผลการปิด"
        rows={data?.by_reason}
        labelOf={reasonLabel}
      />
      <BreakdownCard
        title="แยกตามสินทรัพย์"
        rows={data?.by_asset}
        labelOf={(k) => k || "—"}
      />
    </section>
  );
}

function BreakdownCard({
  title,
  rows,
  labelOf,
}: {
  title: string;
  rows?: PnlBreakdownRow[];
  labelOf: (key: string) => string;
}) {
  const list = rows ?? [];
  const total = list.reduce((s, r) => s + r.pnl, 0);
  return (
    <div className="panel">
      <h2 className="panel-title">{title}</h2>
      {rows === undefined ? (
        <p className="text-slate-500 text-sm">กำลังโหลด...</p>
      ) : list.length === 0 ? (
        <p className="text-slate-500 text-sm">ยังไม่มีไม้ที่ปิดแล้ว</p>
      ) : (
        <ul className="space-y-2 text-sm">
          {list.map((r, i) => (
            <li key={r.key} className="flex justify-between gap-3">
              <span
                className={`truncate ${i === 0 ? "font-bold text-loss" : "text-slate-300"}`}
                title={r.key}
              >
                {labelOf(r.key)}
              </span>
              <span className="shrink-0 text-right">
                <span className="text-slate-400">{r.trades} ไม้</span>
                {" · "}
                <span className={`font-bold ${r.pnl >= 0 ? "text-profit" : "text-loss"}`}>
                  {r.pnl >= 0 ? "+" : "−"}${Math.abs(r.pnl).toFixed(2)}
                </span>
                <span className="text-slate-500"> (W{r.wins}/L{r.losses})</span>
              </span>
            </li>
          ))}
          <li className="flex justify-between gap-3 border-t border-white/10 pt-2 mt-1">
            <span className="text-slate-400">รวม</span>
            <span className="shrink-0 text-right">
              <span className="text-slate-400">
                {list.reduce((s, r) => s + r.trades, 0)} ไม้
              </span>
              {" · "}
              <span className={`font-bold ${total >= 0 ? "text-profit" : "text-loss"}`}>
                {total >= 0 ? "+" : "−"}${Math.abs(total).toFixed(2)}
              </span>
            </span>
          </li>
        </ul>
      )}
    </div>
  );
}
