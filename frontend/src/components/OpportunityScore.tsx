"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import LoadingGraphic from "@/components/LoadingGraphic";
import { api } from "@/lib/api";
import { scoreColor } from "@/lib/format";
import {
  AssetOpportunity,
  CorrelationLinkedPosition,
  CorrelationResponse,
  CorrelationSymbolRisk,
} from "@/lib/types";

const BAND_LABEL: Record<string, string> = {
  very_high: "Very High",
  high: "High",
  medium: "Medium",
  low: "Low",
};

/** ป้ายความเสี่ยง correlation ของแถว (มาจาก symbol_risk ที่ backend คิดให้) */
const RISK_CHIP: Record<string, { label: string; cls: string }> = {
  high: { label: "เสี่ยงซ้ำ", cls: "border-rose-400/40 bg-rose-500/15 text-rose-200" },
  medium: { label: "เฝ้าระวัง", cls: "border-amber-400/30 bg-amber-500/10 text-amber-200" },
};

/** คู่นี้ทับความเสี่ยงกับไม้ที่เปิดอยู่จริงไหม
 *  none = ไม่มีอะไรร่วมเลย / low = สัมพันธ์กันแต่พอร์ตยังห่างเพดานมาก
 *  → ทั้งสองกรณี "ไม่เสี่ยง" จึงไม่ต้องแสดงอะไรบนแถวเลย (ตาม request) */
function isRisky(risk: CorrelationSymbolRisk | null | undefined): boolean {
  return !!risk && (risk.duplicate || risk.level === "high" || risk.level === "medium");
}

function riskChip(risk: CorrelationSymbolRisk | null | undefined) {
  if (!risk || !isRisky(risk)) return null;
  // เปิดคู่นี้อยู่แล้ว = ความเสี่ยงซ้ำชัดเจนที่สุด (auto-trader บล็อกไม้ซ้ำ)
  if (risk.duplicate) {
    return { label: "เปิดอยู่แล้ว", cls: "border-sky-400/40 bg-sky-500/15 text-sky-200" };
  }
  return RISK_CHIP[risk.level] ?? null;
}

/** ชื่อไม้เปิดที่ทับความเสี่ยง (ตัดท้ายเมื่อยาวเกิน) */
function linkedNames(items: CorrelationLinkedPosition[], max = 3): string {
  const names = items.map((p) => p.asset);
  if (!names.length) return "—";
  return names.length <= max
    ? names.join(", ")
    : `${names.slice(0, max).join(", ")} +${names.length - max}`;
}

/** บรรทัดอธิบายไม้เปิดหนึ่งไม้ที่เกี่ยวข้องกับสัญลักษณ์ที่กำลังดู */
function linkedLine(p: CorrelationLinkedPosition): string {
  const bits = [
    `สัมพันธ์ ${Math.round(Math.abs(p.correlation) * 100)}%`,
  ];
  // USD อยู่ในเกือบทุกคู่ — เน้นเฉพาะสกุลอื่นที่ถือร่วมกันจริง
  const shared = p.shared.filter((c) => c !== "USD");
  if (shared.length) bits.push(`ถือ ${shared.join("/")} ร่วม`);
  return `${p.asset} (${p.direction}) — ${bits.join(" · ")}`;
}

/** แถวเดียวของ Opportunity Score — กดที่แถวเพื่อเปิด popup "ที่มาของคะแนน"
 *  (รายละเอียดการคำนวณทุก component จาก score_reasons ที่ scanner เขียนลง DB)
 *  + ส่วน "ความเสี่ยงจากไม้ที่เปิดอยู่" (correlation risk) เมื่อมีไม้เปิดค้าง.
 *  popover แบบ glass ต้อง createPortal ลง document.body เพราะ .panel มี
 *  backdrop-filter ที่ทำให้ position: fixed ภายใน panel ถูก trap
 *  (pattern เดียวกับ LevelMovedBadge หน้า monitor). */
function ScoreRow({ o, gate, tradable, risk, cap }: {
  o: AssetOpportunity;
  gate: number | null;
  tradable: boolean;
  /** ผลประเมิน correlation ของคู่นี้กับไม้ที่เปิดอยู่ (null = ยังไม่มีข้อมูล) */
  risk?: CorrelationSymbolRisk | null;
  /** เพดาน correlation ที่ gate ใช้ (ไว้เทียบ projected) */
  cap?: number;
}) {
  const [pop, setPop] = useState(false);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  const place = useCallback(() => {
    const row = rowRef.current, popEl = popRef.current;
    if (!row) return;
    const r = row.getBoundingClientRect();
    const pw = popEl?.offsetWidth ?? 320;
    const ph = popEl?.offsetHeight ?? 220;
    let left = r.left + r.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    let top = r.bottom + 8;                                       // ใต้แถวเป็นค่าเริ่มต้น
    if (top + ph > window.innerHeight - 8) top = r.top - ph - 8;  // ล่างไม่พอ → เหนือแถว
    if (top < 8) top = 8;
    setPos({ top, left });
  }, []);

  useEffect(() => {
    if (!pop) return;
    place();
    const close = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node | null;
      if (t && (rowRef.current?.contains(t) || popRef.current?.contains(t))) return;
      setPop(false);
    };
    // scroll ภายในรายการเหตุผลของ popup ต้องไม่ปิด popup — ปิดเฉพาะ scroll นอก popup
    const onScroll = (e: Event) => {
      const t = e.target as Node | null;
      if (t && popRef.current?.contains(t)) return;
      setPop(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("touchstart", close, { passive: true });
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("touchstart", close);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [pop, place]);

  const passes = gate != null && o.score >= gate;
  const details = (o.score_reasons?.length ? o.score_reasons : o.reasons).filter(Boolean);
  // เช็คความเสี่ยงของ "คู่นี้" ก่อน แล้วโชว์ทุกอย่างเฉพาะเมื่อเสี่ยงจริง
  const risky = isRisky(risk);
  const chip = riskChip(risk);
  const riskItems = risky ? risk?.with ?? [] : [];
  const capPct = risky && risk && cap && cap > 0
    ? Math.round((risk.projected / cap) * 100)
    : null;
  const overCap = risky && !!risk?.over_cap;

  return (
    <div
      ref={rowRef}
      role="button"
      tabIndex={0}
      aria-expanded={pop}
      aria-label={`Opportunity Score ${o.asset} — แตะเพื่อดูรายละเอียดการคำนวณ`}
      onClick={() => setPop((v) => !v)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setPop((v) => !v); }
      }}
      className="border-b border-slate-800 pb-2 last:border-0 cursor-pointer touch-manipulation select-none"
    >
      <div className="flex items-center justify-between">
        <span className="font-semibold">
          {o.asset} <span className="text-xs font-normal text-slate-500">{BAND_LABEL[o.band] ?? o.band}</span>
          {!tradable && (
            <span className="text-[10px] text-slate-500 border border-white/10 rounded px-1 ml-1.5">ดูอย่างเดียว</span>
          )}
          {chip && (
            <span
              className={`text-[10px] border rounded px-1 ml-1.5 whitespace-nowrap ${chip.cls}`}
              title={risk?.duplicate
                ? "มีไม้เปิดคู่นี้อยู่แล้ว — ระบบกันไม้ซ้ำ"
                : "ซ้ำความเสี่ยงกับไม้ที่เปิดอยู่ — แตะดูรายละเอียด"}
            >
              {chip.label}
            </span>
          )}
        </span>
        <span className="flex items-center gap-1">
          <span className={`font-bold ${scoreColor(o.score)}`}>{o.score.toFixed(0)}%</span>
          <span className={`text-slate-600 transition-transform ${pop ? "rotate-90" : ""}`}>›</span>
        </span>
      </div>
      <div className="relative h-2 bg-slate-800 rounded mt-1 overflow-hidden">
        <div
          className={`h-full rounded ${o.score >= 81 ? "bg-emerald-500" : o.score >= 61 ? "bg-accent" : o.score >= 31 ? "bg-amber-500" : "bg-slate-600"}`}
          style={{ width: `${o.score}%` }}
        />
        {gate != null && (
          <div
            className="absolute top-0 h-2 w-0.5 bg-rose-400/80"
            style={{ left: `${Math.min(100, Math.max(0, gate))}%` }}
            title={`Min Confidence ${gate}%`}
          />
        )}
      </div>
      <div className="flex justify-between text-xs text-slate-500 mt-1">
        <span>
          Confidence {o.score.toFixed(0)}%
          {gate != null && (
            <span className={passes ? "text-emerald-400 ml-1.5" : "text-rose-400 ml-1.5"}>
              ({gate}% {passes ? "ผ่านเกณฑ์" : "ต่ำกว่าเกณฑ์"})
            </span>
          )}
        </span>
        <span>{o.reasons[0]?.slice(0, 60) ?? ""}</span>
      </div>
      {/* ความเสี่ยงเป็นรายคู่เงิน — โผล่เฉพาะคู่ที่ทับไม้เปิดจริง ไม่เสี่ยง = ไม่มีบรรทัดนี้ */}
      {risky && risk && (
        <p className={`mt-1 text-[11px] ${overCap ? "text-rose-300" : "text-amber-300/90"}`}>
          {risk.duplicate ? (
            "เปิดไม้คู่นี้อยู่แล้ว — ระบบกันไม้ซ้ำ ไม่เปิดเพิ่ม"
          ) : (
            <>
              ซ้ำกับ {linkedNames(riskItems)}
              {" — ถ้าเปิดเพิ่มพอร์ตจะเป็น "}
              <b>{risk.projected.toFixed(0)}%</b>
              {capPct != null && ` (${capPct}% ของเพดาน)`}
              {overCap ? " เกินเพดาน จะถูกบล็อก" : ""}
            </>
          )}
        </p>
      )}
      {pop && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label={`รายละเอียดการคำนวณคะแนน ${o.asset}`}
          style={{ position: "fixed", top: pos.top, left: pos.left, width: "min(340px, calc(100vw - 16px))" }}
          className="z-50 rounded-xl border border-slate-700 bg-slate-900/95 backdrop-blur px-3.5 py-3 shadow-xl"
        >
          <div className="flex items-center justify-between">
            <span className="font-semibold text-slate-100">
              {o.asset} <span className="text-xs font-normal text-slate-500">{BAND_LABEL[o.band] ?? o.band}</span>
            </span>
            <span className={`font-bold ${scoreColor(o.score)}`}>{o.score.toFixed(0)}%</span>
          </div>
          <div className="mt-1 text-xs">
            {gate != null ? (
              <span className={passes ? "text-emerald-400" : "text-rose-400"}>
                เกณฑ์ Min Confidence {gate}% — {passes ? "ผ่านเกณฑ์" : "ต่ำกว่าเกณฑ์"}
              </span>
            ) : (
              <span className="text-slate-500">ยังไม่ได้ตั้งเกณฑ์ Min Confidence</span>
            )}
            {!tradable && (
              <span className="block text-slate-500 mt-0.5">อยู่นอก allowed_assets — วิเคราะห์อย่างเดียว ไม่เข้าระบบเทรด</span>
            )}
          </div>
          {risky && risk && (
            <>
              <div className="border-t border-slate-800 my-2" />
              <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 mb-1">
                ความเสี่ยงจากไม้ที่เปิดอยู่
              </p>
              {risk.duplicate && (
                <p className="text-xs text-sky-300">
                  เปิดไม้คู่นี้อยู่แล้ว — ระบบกันไม้ซ้ำ ไม่เปิดเพิ่ม
                </p>
              )}
              {riskItems.length > 0 && (
                <ul className="space-y-1 text-xs text-slate-300">
                  {riskItems.map((p) => (
                    <li key={p.asset} className="flex gap-1.5">
                      <span className="text-slate-600 shrink-0">•</span>
                      <span>{linkedLine(p)}</span>
                    </li>
                  ))}
                </ul>
              )}
              {!risk.duplicate && (
                <p className="mt-1.5 text-xs text-slate-400">
                  ถ้าเปิดคู่นี้ พอร์ตจะมีความสัมพันธ์ {""}
                  <span className={risk.over_cap ? "text-rose-300 font-semibold" : "text-slate-200 font-semibold"}>
                    {risk.projected.toFixed(0)}%
                  </span>
                  {capPct != null && ` (${capPct}% ของเพดาน ${cap}%)`}
                  {" — "}
                  {risk.over_cap
                    ? "เกินเพดาน ระบบจะไม่ยอมเปิดไม้นี้"
                    : "ยังไม่เกินเพดาน แต่เพิ่มความเสี่ยงรวมของพอร์ต"}
                </p>
              )}
              {(risk.hedges?.length ?? 0) > 0 && (
                <p className="mt-1 text-[11px] text-emerald-300/80">
                  กระจายความเสี่ยงกับ {risk.hedges.map((h) => h.asset).join(", ")} (สวนทางกัน)
                </p>
              )}
            </>
          )}
          <div className="border-t border-slate-800 my-2" />
          <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 mb-1">ที่มาของคะแนน</p>
          {details.length ? (
            <ul className="space-y-1 text-xs text-slate-300 max-h-[45vh] overflow-y-auto pr-1">
              {details.map((r, i) => (
                <li key={i} className="flex gap-1.5">
                  <span className="text-slate-600 shrink-0">•</span>
                  <span className="whitespace-pre-line">{r}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-slate-500">ยังไม่มีรายละเอียด — รอ Market Scanner รอบถัดไป</p>
          )}
          <p className="mt-2 text-[10px] text-slate-600">
            คะแนนรวมน้ำหนัก Trend / Momentum / Volatility / ข่าว — Market Scanner อัปเดตทุก 5 นาที
          </p>
        </div>,
        document.body
      )}
    </div>
  );
}

export default function OpportunityScore({ opportunities, loading, error, minConfidence, minConfidenceGold, tradableAssets }: {
  opportunities: AssetOpportunity[];
  loading?: boolean;
  error?: boolean;
  /** Gate threshold (trading_settings.min_confidence) — badge pass/fail per symbol when provided. */
  minConfidence?: number;
  /** Gold-specific gate (min_confidence_gold) — applied to XAUUSD when set; null/undefined = use base gate. */
  minConfidenceGold?: number | null;
  /** Trading whitelist (allowed_assets) — symbols outside it are analysis-only
   * ("ดูอย่างเดียว"); undefined/empty = all tradable (settings not loaded). */
  tradableAssets?: string[];
}) {
  // Client-side paging (หน้าละ 5) — 28 คู่ใน universe ยาวเกินไปสำหรับการอ่านครั้งเดียว
  const PAGE_SIZE = 5;
  const [page, setPage] = useState(1);
  // Correlation risk ของไม้ที่เปิดอยู่ — ขอ backend คิดให้ทั้งชุดพร้อมกัน
  // (symbol_risk) พร้อมเพดานที่ gate ใช้; ล้มเหลว/ยังไม่ login = null แล้ว
  // แถวก็แค่ไม่มีป้าย ไม่ทำให้ panel พัง
  const [corr, setCorr] = useState<CorrelationResponse | null>(null);
  const assetKey = opportunities.map((o) => o.asset).join(",");
  useEffect(() => {
    const assets = assetKey ? assetKey.split(",") : [];
    if (!assets.length) { setCorr(null); return; }
    let alive = true;
    api.tradingCorrelation(assets)
      .then((r) => { if (alive) setCorr(r); })
      .catch(() => { if (alive) setCorr(null); });
    return () => { alive = false; };
  }, [assetKey]);
  if (loading) {
    return <LoadingGraphic message="กำลังโหลดข้อมูลตลาด... (Render cold start อาจใช้เวลาสักครู่)" />;
  }
  if (!opportunities.length) {
    return error
      ? <p className="text-amber-400 text-sm">โหลดข้อมูลไม่สำเร็จ — รีเฟรชหน้าเพื่อลองอีกครั้ง</p>
      : <p className="text-slate-500 text-sm">ยังไม่มีข้อมูล — รอ Market Scanner ทำงานก่อน</p>;
  }
  const gateFor = (asset: string): number | null => {
    if (asset === "XAUUSD" && minConfidenceGold != null) return minConfidenceGold;
    return minConfidence ?? null;
  };
  const isTradable = (asset: string): boolean =>
    !tradableAssets?.length || tradableAssets.includes(asset);
  const totalPages = Math.max(1, Math.ceil(opportunities.length / PAGE_SIZE));
  // clamp แทน reset — dashboard poll ทุก 30 วิ อย่าบังคับผู้ใช้กลับหน้า 1 ตอนข้อมูลรีเฟรช
  const safePage = Math.min(page, totalPages);
  const pageRows = opportunities.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);
  // แถบสรุปความเสี่ยงพอร์ต — แสดงเมื่อ "มีไม้เปิดค้าง" เท่านั้น (พอร์ตว่าง =
  // ไม่มีความเสี่ยงซ้ำให้เตือน) และ cap สูงสุด 0 กันหารศูนย์
  // 60% นี้ต้องตรงกับ CorrelationEngine.NEAR_CAP_RATIO ฝั่ง backend
  const riskMap = corr?.symbol_risk ?? null;
  const cap = corr?.correlation_cap ?? null;
  const book = corr?.open_positions ?? [];
  const capPct = corr && cap && cap > 0
    ? Math.min(100, Math.round((corr.portfolio_correlation / cap) * 100))
    : null;
  const overCap = !!(corr && cap && corr.portfolio_correlation > cap);
  const nearCap = capPct != null && !overCap && capPct >= 60;
  // "รายตัว" = คู่ที่สแกนเจอและทับความเสี่ยงกับไม้เปิดจริงเท่านั้น
  // (คู่ที่ไม่เสี่ยงไม่ถูกใส่ในลิสต์เลย — ตาม request)
  const riskyRows: { asset: string; risk: CorrelationSymbolRisk }[] = [];
  if (riskMap) {
    for (const o of opportunities) {
      const r = riskMap[o.asset];
      if (isRisky(r)) riskyRows.push({ asset: o.asset, risk: r });
    }
  }
  return (
    <div>
    {book.length > 0 && corr && (
      <div className="mb-3 rounded-xl border border-white/10 bg-white/[0.03] px-2.5 py-2">
        <div className="flex items-center justify-between gap-2 text-[11px]">
          <span className="text-slate-400">ความเสี่ยงความสัมพันธ์พอร์ต</span>
          <span className={`font-semibold ${overCap ? "text-rose-300" : nearCap ? "text-amber-300" : "text-slate-200"}`}>
            {corr.portfolio_correlation.toFixed(0)}%
            {cap != null && <span className="font-normal text-slate-500"> / เพดาน {cap}%</span>}
          </span>
        </div>
        <div className="relative h-1.5 bg-slate-800 rounded mt-1 overflow-hidden" title={`portfolio correlation ${corr.portfolio_correlation} / cap ${cap ?? "-"}`}>
          <div
            className={`h-full rounded ${overCap ? "bg-rose-500" : nearCap ? "bg-amber-500" : "bg-emerald-500/70"}`}
            style={{ width: `${capPct ?? 0}%` }}
          />
        </div>
        {riskyRows.length > 0 ? (
          <>
            <p className="mt-1.5 text-[11px] font-semibold text-slate-500">
              คู่ที่สแกนเจอและทับความเสี่ยงไม้เปิด ({riskyRows.length}/{opportunities.length})
            </p>
            <ul className="mt-0.5 space-y-0.5">
              {riskyRows.slice(0, 6).map(({ asset, risk }) => (
                <li key={asset} className="flex items-center justify-between gap-2 text-[11px]">
                  <span className="font-medium text-slate-300">{asset}</span>
                  <span className={risk.over_cap ? "text-rose-300" : "text-amber-300"}>
                    {risk.duplicate
                      ? "เปิดไม้นี้อยู่แล้ว"
                      : `ถ้าเปิดเพิ่ม → พอร์ต ${risk.projected.toFixed(0)}% · ทับ ${risk.with.length} ไม้`}
                  </span>
                </li>
              ))}
            </ul>
            {riskyRows.length > 6 && (
              <p className="mt-0.5 text-[11px] text-slate-600">+ อีก {riskyRows.length - 6} คู่</p>
            )}
          </>
        ) : (
          <p className="mt-1.5 text-[11px] text-slate-500">
            ไม่มีคู่ที่สแกนเจอทับความเสี่ยงกับไม้เปิด {book.length} ไม้
          </p>
        )}
        <p className={`mt-0.5 text-[11px] ${overCap ? "text-rose-300" : nearCap ? "text-amber-300" : "text-slate-500"}`}>
          {overCap
            ? "เกินเพดาน — ระบบจะไม่เปิดไม้เพิ่มจนกว่าจะปิดบางส่วน"
            : nearCap
              ? "ใกล้เพดาน — เปิดคู่ที่สัมพันธ์กันอีกไม่กี่ไม้จะติดเพดาน"
              : "ยังห่างเพดาน — รายการด้านบนคือคู่ที่ยังทับไม้เปิดอยู่"}
        </p>
      </div>
    )}
    <div className="space-y-3">
      {pageRows.map((o) => (
        <ScoreRow key={o.asset} o={o} gate={gateFor(o.asset)} tradable={isTradable(o.asset)}
          risk={riskMap?.[o.asset] ?? null} cap={cap ?? undefined} />
      ))}
    </div>
    {opportunities.length > 0 && (
      <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
        <p className="text-xs text-slate-500">
          หน้า {safePage}/{totalPages} · แสดง {pageRows.length} จาก {opportunities.length} รายการ
        </p>
        {totalPages > 1 && (
          <div className="flex items-center gap-2">
            <button onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={safePage <= 1}
              className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
              ก่อนหน้า
            </button>
            <span className="text-xs text-slate-400">{safePage} / {totalPages}</span>
            <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={safePage >= totalPages}
              className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
              ถัดไป
            </button>
          </div>
        )}
      </div>
    )}
    </div>
  );
}
