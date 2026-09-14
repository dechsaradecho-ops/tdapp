"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import Icon from "@/components/Icon";
import LoadingGraphic from "@/components/LoadingGraphic";
import {
  NewsLog,
  NewsLogsResponse,
  QuoteApiLog,
  QuoteLogSummary,
  QuoteTestResult,
  RiskAuditRequest,
  RiskEventLog,
  RiskLogsResponse,
  SchedulerLog,
  SchedulerLogsResponse,
  SchedulerJobStat,
  SignalLog,
  SignalLogsResponse,
} from "@/lib/types";

/** สีของ badge ตามสถานะ call */
function statusBadge(status: string) {
  return status === "success"
    ? "bg-emerald-500/15 text-emerald-400"
    : "bg-red-500/15 text-red-400";
}

/**
 * แยก "k=v, k=v" ทั้งบรรทัดของ guard detail เป็น map ของข้อความดิบ
 *
 * ทำไมไม่ split(",") ตรง ๆ: ตัวนับเป็นคู่ k=v แต่ list ของ symbol
 * (sl_assets / closed_assets / skip_assets) เป็นข้อความยาวที่อาจมีอะไรก็ได้
 * จึงใช้ lookahead หา ", key=" เป็นตัวคั่นจริงเท่านั้น → ค่าที่มี comma
 * ข้างในยังอยู่ครบ (never throws)
 */
function splitGuardDetail(detail: string): Record<string, string> {
  const out: Record<string, string> = {};
  try {
    const re = /([a-z_]+)\s*=\s*(.*?)(?=,\s*[a-z_]+\s*=|$)/gi;
    const s = String(detail || "");
    let m: RegExpExecArray | null;
    // exec loop (ไม่ใช้ matchAll: target ของโปรเจกต์ตํ่ากว่า es2015)
    while ((m = re.exec(s)) !== null) {
      out[m[1].toLowerCase()] = m[2].trim();
      if (m.index === re.lastIndex) re.lastIndex += 1; // กัน loop ค้างบน match ว่าง
    }
  } catch { /* never throws */ }
  return out;
}

/** ตัวเลขจาก guard detail "checked=2, closed=0, moved_sl=1, ..." (never throws) */
function parseGuardDetail(detail: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(splitGuardDetail(detail))) {
    if (/^-?\d+$/.test(v)) out[k] = parseInt(v, 10) || 0;
  }
  return out;
}

/**
 * ความหมายของตัวนับแต่ละตัวใน scheduler_runs.detail ของ position_guard
 *
 * เดิมหน้า Guard โชว์สตริงดิบ "checked=4, closed=0, moved_sl=2, ..." ทำให้
 * "moved_sl=2" ไม่สื่อว่า 2 คืออะไร ใครขยับ และทำไม closed เป็น 0
 */
const GUARD_FIELDS: { label: string; key: string; hint: string }[] = [
  { label: "ตรวจไม้", key: "checked", hint: "จำนวนไม้เปิดที่ guard ไล่ตรวจในรอบนี้" },
  { label: "ขยับ SL", key: "moved_sl", hint: "เลื่อน stop กระชับขึ้นสำเร็จ: breakeven / trailing / R-ladder" },
  { label: "ปิดไม้", key: "closed", hint: "รวมทุกสาเหตุ: SL · TP · Smart Exit · time stop · kill switch" },
  { label: "ปิดบางส่วน TP1", key: "partial_closed", hint: "ปิดบางส่วนตาม partial_trigger_r แล้วที่เหลือปล่อย trailing" },
  { label: "smart ปิด", key: "smart_closed", hint: "Smart Exit สั่ง CLOSE แล้วปิดสำเร็จจริง" },
  { label: "smart แบ่งปิด", key: "smart_partials", hint: "Smart Exit สั่ง PARTIAL_25/50 แล้วแบ่งปิดสำเร็จ" },
  { label: "smart ข้าม", key: "smart_skipped", hint: "engine สั่งปิดแต่ทำไม่ได้ (ไม่มีราคา / broker ปฏิเสธ / TP1 ทำแล้ว) — ไม่ใช่การไม่ทำอะไรเงียบ ๆ" },
  { label: "ฉุกเฉิน", key: "emergency_closed", hint: "kill switch เข้าเงื่อนไข → ปิดไม้ทันที" },
  { label: "เลื่อนฉุกเฉิน", key: "emergency_held", hint: "kill switch เข้าเงื่อนไข แต่มีคำขอขยายลิมิตที่รอการยืนยันอยู่ → เลื่อนการปิดไม้ออกไป (SL/TP ยังทำงานปกติ) จนกว่าจะได้คำตอบ หรือครบช่วงยืนยัน (kill_expand_ttl_min) แล้วระบบขยายลิมิตให้อัตโนมัติ — guard จะปิดไม้เฉพาะเมื่อยังเกินลิมิตใหม่เท่านั้น ถ้าขยายอัตโนมัติไม่สำเร็จ (เขียน DB ไม่ได้) จะยังเลื่อนต่อไปโดยไม่ปิดไม้ และเตือนซ้ำทุก ~6 ชม." },
  { label: "ข้ามทั้งรอบ", key: "skipped_prev_running", hint: "รอบก่อนยังไม่จบ → รอบนี้ถูกข้าม ไม่ได้ตรวจอะไรเลย (รอบไม่หายไปแล้ว)" },
];

/** token เหตุผลการปิดไม้ (ส่วนหลัง ":") → ภาษาไทย */
const GUARD_CLOSE_REASONS: Record<string, string> = {
  sl: "ตัดขาดทุน (SL)",
  tp: "ปิดกำไร (TP)",
  tp1: "ปิดบางส่วน TP1",
  smart: "Smart Exit ปิดทั้งไม้",
  smart25: "Smart Exit แบ่งปิด 25%",
  smart50: "Smart Exit แบ่งปิด 50%",
  time: "ครบกำหนดถือ (time stop)",
  kill: "kill switch (ฉุกเฉิน)",
};

/** token เหตุผลที่ข้าม → ภาษาไทย */
const GUARD_SKIP_REASONS: Record<string, string> = {
  no_snapshot: "ไม่มีราคา (blind HOLD)",
  not_applied: "สั่งปิดแล้วไม่สำเร็จ",
  eval_error: "ประเมินไม่สำเร็จ",
};

/** "EURCHF@0.93624>0.94337" → {asset, oldSl, newSl} (never throws)
 *  รูปแบบเดิม "EURCHF@0.94337" (ไม่มีค่าเดิม) → oldSl = "" */
function parseGuardSlMove(raw: string): { asset: string; oldSl: string; newSl: string } | null {
  const s = String(raw || "").trim();
  if (!s) return null;
  const at = s.indexOf("@");
  if (at < 0) return { asset: s, oldSl: "", newSl: "" };
  const asset = s.slice(0, at);
  const parts = s.slice(at + 1).split(">");
  if (parts.length > 1) {
    return { asset, oldSl: parts[0].trim(), newSl: parts[1].trim() };
  }
  return { asset, oldSl: "", newSl: parts[0].trim() };
}

/** ตัด float noise ของส่วนต่างราคา: 0.007130000000000046 → "0.00713" */
function fmtAuditNum(v: number): string {
  return Number.isFinite(v) ? String(Number(v.toPrecision(6))) : "—";
}

/**
 * "EURCHF@0.93624>0.94337" → "EURCHF → SL 0.93624 → 0.94337"
 *
 * บอกทั้งคู่เงินและ "ขยับจากเท่าไร" เพื่อไม่ต้องเปิด journal หาเอง
 * (รองรับข้อมูลเก่าที่มีแต่ SL ปลายทาง)
 */
function guardSlLabel(raw: string): string {
  const m = parseGuardSlMove(raw);
  if (!m) return "";
  if (m.oldSl && m.newSl) return `${m.asset} → SL ${m.oldSl} → ${m.newSl}`;
  const one = m.newSl || m.oldSl;
  return one ? `${m.asset} → SL ${one}` : m.asset;
}

/** "EURCHF:sl@1.2345" → "EURCHF · ตัดขาดทุน (SL) @1.2345" */
function guardClosedLabel(raw: string): string {
  const s = String(raw);
  const cut = s.indexOf(":");
  const asset = cut >= 0 ? s.slice(0, cut) : s;
  const rest = cut >= 0 ? s.slice(cut + 1) : "";
  const [reason, price] = rest.split("@");
  const th = GUARD_CLOSE_REASONS[reason] || reason || "ปิดไม้";
  return price ? `${asset} · ${th} @${price}` : `${asset} · ${th}`;
}

/** "GBPCHF:no_snapshot" → "GBPCHF · ข้าม: ไม่มีราคา (blind HOLD)" */
function guardSkipLabel(raw: string): string {
  const s = String(raw);
  const cut = s.indexOf(":");
  const asset = cut >= 0 ? s.slice(0, cut) : s;
  const reason = cut >= 0 ? s.slice(cut + 1) : "";
  return `${asset} · ข้าม: ${GUARD_SKIP_REASONS[reason] || reason || "ไม่ทราบสาเหตุ"}`;
}

/** token "…" = บรรทัดถูกตัด (_summarize limit) → ห้ามใช้ค่าที่อาจขาดกลาง */
const GUARD_TRUNCATED = "…";

/**
 * แยก list ชื่อคู่เงิน "A@1>2;B@3>4" เป็นรายการ
 * ทิ้ง token สุดท้ายที่ติด "…" เพราะราคาถูกตัดครึ่ง (โชว์ผิดแย่กว่าไม่โชว์)
 */
function guardChipItems(rawList: string): string[] {
  return String(rawList || "")
    .split(";")
    .filter((it) => it.trim() && !it.includes(GUARD_TRUNCATED));
}

/** ชิปดอกจิกของ symbol ในช่องรายละเอียด guard */
function GuardChips({ items, tone }: { items: string[]; tone: string }) {
  if (!items.length) return null;
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((it, i) => (
        <span key={i} className={`text-[11px] px-1.5 py-0.5 rounded border ${tone}`}>
          {it}
        </span>
      ))}
    </span>
  );
}

/**
 * ชิป "ขยับ SL" — โชว์ SL เดิม → SL ใหม่ ในบรรทัดเดียว
 * และกดเพื่อเปิด popup อธิบาย (portal ไป body)
 *
 * WHY portal: `.panel` มี backdrop-filter → เป็น containing block ของ
 * position:fixed ทำให้ popup ที่ไม่ portal จม/เพี้ยน (pattern เดียวกับ
 * tap-popover ของหน้า monitor)
 */
function GuardSlChip({ raw }: { raw: string }) {
  const [pop, setPop] = useState(false);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  const place = useCallback(() => {
    const btn = btnRef.current, el = popRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const pw = el?.offsetWidth ?? 280;
    const ph = el?.offsetHeight ?? 140;
    let left = r.left + r.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    let top = r.top - ph - 8;
    if (top < 8) top = r.bottom + 8;
    setPos({ top, left });
  }, []);

  useEffect(() => {
    if (!pop) return;
    place();
    const close = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node | null;
      if (t && (btnRef.current?.contains(t) || popRef.current?.contains(t))) return;
      setPop(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("touchstart", close, { passive: true });
    const onScrollOrResize = () => setPop(false);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("touchstart", close);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, [pop, place]);

  const m = parseGuardSlMove(raw);
  if (!m) return null;
  const o = parseFloat(m.oldSl);
  const n = parseFloat(m.newSl);
  const both = Number.isFinite(o) && Number.isFinite(n);
  const delta = both ? Math.abs(n - o) : NaN;
  const pct = both && o !== 0 ? (Math.abs(n - o) / Math.abs(o)) * 100 : NaN;
  const label = guardSlLabel(raw);
  const title = both
    ? `${m.asset} — ขยับ SL\nเดิม ${m.oldSl} → ใหม่ ${m.newSl}\nกระชับขึ้น ${fmtAuditNum(delta)}`
    : label;
  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={() => setPop((v) => !v)}
        title={title}
        aria-label={title}
        aria-expanded={pop}
        className="text-[11px] px-1.5 py-0.5 rounded border border-amber-400/30 bg-amber-500/10 text-amber-200 touch-manipulation text-left"
      >
        {label}
      </button>
      {pop && createPortal(
        <div
          ref={popRef}
          role="tooltip"
          style={{ position: "fixed", top: pos.top, left: pos.left, maxWidth: "min(320px, calc(100vw - 16px))" }}
          className="z-50 rounded-lg border border-slate-700 bg-slate-900/95 backdrop-blur px-3 py-2 shadow-xl text-xs leading-relaxed text-slate-200"
        >
          <div className="font-bold mb-1">ขยับ Stop Loss — {m.asset}</div>
          <div className="text-amber-200">
            {m.oldSl && m.newSl ? (
              <>SL เดิม {m.oldSl} <span className="text-slate-500">→</span> SL ใหม่ {m.newSl}</>
            ) : (
              <>
                SL ใหม่ {m.newSl || m.oldSl}{" "}
                <span className="text-slate-500">
                  (รอบที่บันทึกก่อนมีฟีเจอร์นี้ — ไม่ได้เก็บค่าเดิม)
                </span>
              </>
            )}
          </div>
          {both && (
            <div className="text-slate-400 mt-0.5">
              กระชับขึ้น {fmtAuditNum(delta)}
              {Number.isFinite(pct) ? ` (${fmtNum(pct, 2)}%)` : ""}
            </div>
          )}
          <div className="mt-1 text-slate-400">
            guard ขยับ stop ให้แคบลงเท่านั้น (breakeven / trailing / R-ladder)
            — ราคานี้ถูกส่งไป broker จริงแล้ว ไม่ใช่แค่ที่แสดงบนจอ
          </div>
        </div>,
        document.body
      )}
    </>
  );
}

/** รายการชิป "ขยับ SL" (กดดู popup ได้ทีละอัน) */
function GuardSlChips({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((it, i) => (
        <GuardSlChip key={i} raw={it} />
      ))}
    </span>
  );
}

/**
 * ช่อง "รายละเอียดรอบนี้" ของตาราง guard — สรุปเป็น "บรรทัดเดียว" แล้วกดเปิด popup
 *
 * เดิม cell นี้แสดงหลายบรรทัดต่อแถว (ประโยค + ชิปดอกจิก + ค่าดิบ font-mono)
 * ทำให้ตารางสูงไม่เท่ากัน และตัวเลขอย่าง moved_sl ก็ต้องเลื่อนไปเปิด details
 * อ่านวิธีแปลข้างล่าง ตอนนี้แถวโชว์สรุปสั้นสีสว่าง (ตรวจ 4 · ขยับ SL 2 · ปิด 0)
 * แล้วกดที่แถวเพื่อเปิด popup ที่อธิบายทุกตัวนับ + รายชื่อคู่เงิน + ค่าดิบ
 *
 * WHY portal: `.panel` มี backdrop-filter → เป็น containing block ของ
 * position:fixed จึงต้อง portal ไป document.body (pattern เดียวกับ GuardSlChip)
 */
function GuardDetailCell({ detail, status, createdAt, durationMs }: {
  detail: string | null;
  status: string;
  /** เวลาของรอบ (โชว์หัว popup) */
  createdAt?: string | null;
  /** ระยะเวลารอบ (ms) — โชว์หัว popup */
  durationMs?: number | null;
}) {
  const [pop, setPop] = useState(false);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  const place = useCallback(() => {
    const btn = btnRef.current, el = popRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const pw = el?.offsetWidth ?? 340;
    const ph = el?.offsetHeight ?? 300;
    let left = r.left + r.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    let top = r.top - ph - 8;
    if (top < 8) top = r.bottom + 8;
    // popup สูงได้ถึง 80vh — กันล้นขอบล่างของจอ
    if (top + ph > window.innerHeight - 8) top = Math.max(8, window.innerHeight - ph - 8);
    setPos({ top, left });
  }, []);

  useEffect(() => {
    if (!pop) return;
    place();
    const close = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node | null;
      if (t && (btnRef.current?.contains(t) || popRef.current?.contains(t))) return;
      setPop(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("touchstart", close, { passive: true });
    const onScrollOrResize = () => setPop(false);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("touchstart", close);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, [pop, place]);

  const raw = String(detail || "");
  const kv = splitGuardDetail(raw);
  const num = (k: string) => {
    const v = parseInt(kv[k] || "0", 10);
    return Number.isFinite(v) ? v : 0;
  };

  const skipped = num("skipped_prev_running") > 0;
  const failed = status === "error";
  const empty = !raw.trim();
  const slMoves = guardChipItems(kv.sl_assets);
  const closedList = guardChipItems(kv.closed_assets);
  const skipList = guardChipItems(kv.skip_assets);
  const truncated = raw.includes(GUARD_TRUNCATED);
  // อธิบายเฉพาะตัวนับที่มีค่าจริง (0 = ไม่เกิดเหตุการณ์นั้นรอบนี้)
  const fieldRows = GUARD_FIELDS.filter((f) => num(f.key) > 0);
  const extras: string[] = [];
  if (num("partial_closed") > 0) extras.push(`ปิดบางส่วน TP1 ${num("partial_closed")}`);
  if (num("smart_closed") > 0) extras.push(`smart ปิด ${num("smart_closed")}`);
  if (num("smart_partials") > 0) extras.push(`smart แบ่งปิด ${num("smart_partials")}`);
  if (num("smart_skipped") > 0) extras.push(`smart ข้าม ${num("smart_skipped")}`);
  if (num("emergency_closed") > 0) extras.push(`⚠ ฉุกเฉิน ${num("emergency_closed")}`);
  if (num("emergency_held") > 0) extras.push(`⏸ เลื่อนฉุกเฉิน ${num("emergency_held")}`);

  // โทนสีของแถวสรุป: ข้าม (เหลือง) · ล้มเหลว (แดง) · ไม่มีข้อมูล (เทา) · ปกติ (ขาว)
  const tone = skipped
    ? "border-amber-400/50 bg-amber-500/15 text-amber-200"
    : failed
      ? "border-rose-400/50 bg-rose-500/15 text-rose-200"
      : empty
        ? "border-slate-400/40 bg-slate-500/15 text-slate-200"
        : "border-white/15 bg-white/[0.07] text-slate-100 hover:bg-white/[0.11]";

  return (
    <div className="text-xs">
      <button
        ref={btnRef}
        type="button"
        onClick={() => setPop((v) => !v)}
        aria-expanded={pop}
        title="กดเพื่อดูรายละเอียดรอบนี้แบบเต็ม"
        className={`inline-flex w-full min-w-0 items-center gap-1.5 rounded-lg border px-2 py-1 text-left touch-manipulation ${tone}`}
      >
        <span className="min-w-0 flex-1 truncate">
          {skipped ? (
            <>ข้ามรอบนี้ <span className="text-amber-300/80">— รอบก่อนยังไม่จบ</span></>
          ) : failed ? (
            <>รอบนี้ล้มเหลว <span className="text-rose-300/80">— ดูช่อง Error</span></>
          ) : empty ? (
            <>ไม่มีรายละเอียด <span className="text-slate-400">— รอบถูกยกเลิก/หมดเวลา</span></>
          ) : (
            <>
              ตรวจ <b className={num("checked") > 0 ? "text-slate-50" : "text-slate-400"}>{num("checked")}</b>
              <span className="text-slate-500"> · </span>
              ขยับ SL <b className={num("moved_sl") > 0 ? "text-amber-300" : "text-slate-400"}>{num("moved_sl")}</b>
              <span className="text-slate-500"> · </span>
              ปิด <b className={num("closed") > 0 ? "text-sky-300" : "text-slate-400"}>{num("closed")}</b>
              {extras.length > 0 && <span className="text-slate-300"> · {extras.join(" · ")}</span>}
              {truncated && <span className="text-amber-300"> · ⚠ รายการถูกตัดท้าย</span>}
            </>
          )}
        </span>
        <span className={`shrink-0 text-slate-400 transition-transform ${pop ? "rotate-90" : ""}`}>›</span>
      </button>
      {pop && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label="รายละเอียดรอบนี้ของ position_guard"
          style={{ position: "fixed", top: pos.top, left: pos.left, width: "min(360px, calc(100vw - 16px))" }}
          className="z-50 max-h-[80vh] overflow-y-auto rounded-xl border border-slate-600 bg-slate-900/95 backdrop-blur px-3.5 py-3 text-xs leading-relaxed text-slate-100 shadow-xl"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold text-slate-50">รายละเอียดรอบนี้</span>
            <span className={`text-[11px] px-1.5 py-0.5 rounded ${skipped ? "bg-amber-500/20 text-amber-200" : failed ? "bg-red-500/20 text-red-200" : "bg-emerald-500/20 text-emerald-200"}`}>
              {skipped ? "ข้าม" : failed ? "error" : "ok"}
            </span>
          </div>
          <p className="mt-0.5 text-[11px] text-slate-300">
            {createdAt ? new Date(createdAt).toLocaleString("th-TH", { hour12: false }) : "—"}
            {durationMs != null && <> · ใช้ <b className="text-slate-100">{durationMs}</b> ms</>}
          </p>
          <div className="border-t border-slate-700 my-2" />
          {skipped ? (
            <p className="text-amber-200">
              รอบนี้ <b>ถูกข้าม</b> เพราะรอบก่อนยังไม่จบ — ไม่ได้ตรวจไม้เลย (จำนวนรอบยังนับครบ ไม่หายไป)
            </p>
          ) : failed ? (
            <p className="text-rose-200">
              รอบนี้ <b>ล้มเหลว</b> ระหว่างทำงาน — ข้อความ error อยู่ในคอลัมน์ Error ของแถวนี้
            </p>
          ) : empty ? (
            <p className="text-slate-200">
              รอบนี้ถูกยกเลิก/หมดเวลาก่อนบันทึกผล — ดู heartbeat ด้านบนประกอบ
            </p>
          ) : (
            <>
              <p className="text-slate-50">
                ตรวจ <b>{num("checked")}</b> ไม้
                <span className="text-slate-500"> · </span>
                ขยับ SL <b className="text-amber-300">{num("moved_sl")}</b>
                <span className="text-slate-500"> · </span>
                ปิดไม้ <b className="text-sky-300">{num("closed")}</b>
              </p>
              <p className="mt-1 text-[11px] text-slate-300">
                guard ไล่ตรวจไม้เปิดทุกรอบ ถ้าราคาไปทางบวกจะเลื่อน stop ให้กระชับขึ้น
                (breakeven / trailing / R-ladder) แล้วส่งราคานั้นไป broker จริง · ปิด 0
                ไม่ได้แปลว่าพัง เพราะ SL/TP ที่ตั้งไว้ทำงานที่ฝั่ง broker เอง
              </p>
            </>
          )}
          {fieldRows.length > 0 && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400 mb-1">ตัวนับรอบนี้</p>
              <ul className="space-y-1">
                {fieldRows.map((f) => (
                  <li key={f.key} className="flex gap-1.5">
                    <span className="text-slate-500 shrink-0">•</span>
                    <span>
                      <b className="text-slate-50">{f.label}</b>
                      <span className="text-slate-100"> = {num(f.key)}</span>
                      <span className="text-slate-400"> — {f.hint}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {(slMoves.length > 0 || closedList.length > 0 || skipList.length > 0) && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400 mb-1.5">ไม้ที่ถูกแตะในรอบนี้</p>
              {slMoves.length > 0 && (
                <div className="mb-2">
                  <p className="text-[11px] font-medium text-amber-300 mb-1">
                    ขยับ SL {slMoves.length} ไม้ — กดที่ชิปเพื่อดูส่วนต่าง
                  </p>
                  <GuardSlChips items={slMoves} />
                </div>
              )}
              {closedList.length > 0 && (
                <div className="mb-2">
                  <p className="text-[11px] font-medium text-sky-300 mb-1">ปิด/แบ่งปิด {closedList.length} ไม้</p>
                  <GuardChips
                    items={closedList.map(guardClosedLabel)}
                    tone="border-sky-400/40 bg-sky-500/15 text-sky-100"
                  />
                </div>
              )}
              {skipList.length > 0 && (
                <div>
                  <p className="text-[11px] font-medium text-slate-300 mb-1">สั่งแล้วข้าม {skipList.length} ไม้</p>
                  <GuardChips
                    items={skipList.map(guardSkipLabel)}
                    tone="border-slate-400/40 bg-slate-500/15 text-slate-200"
                  />
                </div>
              )}
            </>
          )}
          {truncated && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <p className="text-amber-200">
                รายการยาวเกิน 480 ตัวอักษร — ข้อความถูกตัดท้าย (แสดงไม้ไม่ครบทุกตัว)
              </p>
            </>
          )}
          {raw.trim() && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <p className="text-[10px] font-mono break-all text-slate-400" title="ค่าดิบจาก scheduler_runs.detail">
                {raw}
              </p>
            </>
          )}
        </div>,
        document.body
      )}
    </div>
  );
}

/** เวลาแบบไทย (ว่าง = "—") สำหรับตาราง audit */
function auditTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("th-TH", { hour12: false });
  } catch {
    return "—";
  }
}

/** ป้ายชื่อเหตุการณ์ความเสี่ยง (risk_events.event_type) เป็นภาษาไทย */
function riskEventLabel(t: string): string {
  if (t === "limit_breach") return "kill switch เข้าเงื่อนไข";
  if (t === "limit_expanded") return "อนุมัติขยายลิมิต";
  if (t === "limit_expand_rejected") return "ปฏิเสธคำขอขยายลิมิต";
  return t || "—";
}

/** ชื่อสั้นสำหรับปุ่มกรอง */
function riskEventShort(t: string): string {
  if (t === "limit_breach") return "kill switch";
  if (t === "limit_expanded") return "อนุมัติขยาย";
  if (t === "limit_expand_rejected") return "ปฏิเสธขยาย";
  return t;
}

function riskEventTone(t: string): string {
  if (t === "limit_breach") return "bg-rose-500/15 text-rose-300";
  if (t === "limit_expanded") return "bg-amber-500/15 text-amber-300";
  if (t === "limit_expand_rejected") return "bg-sky-500/15 text-sky-300";
  return "bg-slate-500/15 text-slate-300";
}

function riskStatusLabel(s: string): string {
  if (s === "approved") return "อนุมัติแล้ว";
  if (s === "rejected") return "ปฏิเสธแล้ว";
  if (s === "pending") return "รอเจ้าของตอบ";
  if (s === "expired") return "หมดเวลายืนยัน";
  return s || "—";
}

function riskStatusTone(s: string): string {
  if (s === "approved") return "bg-amber-500/15 text-amber-300";
  if (s === "rejected") return "bg-sky-500/15 text-sky-300";
  if (s === "pending") return "bg-emerald-500/15 text-emerald-300";
  return "bg-slate-500/15 text-slate-300";
}

/** ใครเป็นคนตัดสินใจ (kill_expand_requests.decided_by) */
function decidedByLabel(by: string): string {
  const v = (by || "").trim();
  if (!v) return "ยังไม่มีคนตัดสิน";
  if (v.startsWith("line")) return "เจ้าของกดปุ่มใน LINE";
  if (v === "ui") return "เจ้าของกดในหน้าเว็บ";
  if (v.startsWith("auto")) return "ระบบขยายให้อัตโนมัติ (หมดเวลายืนยัน)";
  return v;
}

/** "k=v · k=v" จาก object ใด ๆ (ค่าที่เป็น object → JSON) — ไม่ throw */
function kvLine(o: Record<string, unknown>): string {
  try {
    return Object.entries(o)
      .map(([k, v]) => `${k}=${v && typeof v === "object" ? JSON.stringify(v) : String(v)}`)
      .join(" · ");
  } catch {
    return "";
  }
}

/**
 * ช่อง "รายละเอียดการตัดสินใจ" ของแถว risk_events
 *
 * ย่อบรรทัดเดียวแล้วเปิด popup แบบ portal (เหมือน GuardDetailCell) — ต้อง
 * createPortal(document.body) เพราะ `.panel` มี backdrop-filter ซึ่งกลายเป็น
 * containing block ของ position: fixed → popup จะถูกตัดขอบถ้าไม่ portal
 */
function AuditDetailCell({ ev }: { ev: RiskEventLog }) {
  const [pop, setPop] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const d = ev.detail ?? {};
  const isExpand = ev.event_type === "limit_expanded" || ev.event_type === "limit_expand_rejected";
  const triggers = Array.isArray(d.triggers) ? (d.triggers as unknown[]) : [];
  const breaches = Array.isArray(d.breaches) ? (d.breaches as string[]) : [];
  const message = typeof d.message === "string" ? d.message : "";
  const level = typeof d.risk_level === "string" ? d.risk_level : "";
  const numOrNull = (v: unknown): number | null => {
    if (v === null || v === undefined || v === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const before = numOrNull(d.limit_before);
  const after = numOrNull(d.limit_after);

  useEffect(() => {
    if (!pop) return;
    const r = btnRef.current?.getBoundingClientRect();
    if (r) {
      const w = Math.min(360, window.innerWidth - 16);
      const left = Math.min(r.left, Math.max(8, window.innerWidth - w - 8));
      const ph = popRef.current?.offsetHeight ?? 260;
      let top = r.bottom + 6;
      if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
      setPos({ top, left });
    }
    const close = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node | null;
      if (t && (btnRef.current?.contains(t) || popRef.current?.contains(t))) return;
      setPop(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("touchstart", close, { passive: true });
    const onMove = () => setPop(false);
    window.addEventListener("scroll", onMove, true);
    window.addEventListener("resize", onMove);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("touchstart", close);
      window.removeEventListener("scroll", onMove, true);
      window.removeEventListener("resize", onMove);
    };
  }, [pop]);

  return (
    <div className="text-xs">
      <button
        ref={btnRef}
        type="button"
        onClick={() => setPop((v) => !v)}
        aria-expanded={pop}
        title="กดเพื่อดูรายละเอียดการตัดสินใจครั้งนี้แบบเต็ม"
        className="inline-flex w-full min-w-0 items-center gap-1.5 rounded-lg border border-white/15 bg-white/[0.07] px-2 py-1 text-left touch-manipulation hover:bg-white/[0.11]"
      >
        <span className="min-w-0 flex-1 truncate">
          {isExpand ? (
            <>
              ลิมิต{" "}
              <b className="text-slate-50">
                {before !== null && after !== null
                  ? `${fmtNum(before, 1)}% → ${fmtNum(after, 1)}%`
                  : "—"}
              </b>
              {d.approved === false && <span className="text-slate-400"> · ไม่ขยาย (คงลิมิตเดิม)</span>}
              {d.approved === true && <span className="text-emerald-300"> · ขยายแล้ว ไม่ปิดไม้</span>}
              {triggers.length > 0 && <span className="text-slate-400"> · เข้าเงื่อนไข {triggers.length} ข้อ</span>}
            </>
          ) : (
            <>
              {level && (
                <>
                  ระดับ <b className="text-rose-300">{level}</b>
                  <span className="text-slate-500"> · </span>
                </>
              )}
              {breaches.length > 0 ? breaches.slice(0, 2).join(" · ") : (message || "ลิมิตถูกแตะ")}
              {breaches.length > 2 && <span className="text-slate-400"> +{breaches.length - 2}</span>}
            </>
          )}
        </span>
        <span className={`shrink-0 text-slate-400 transition-transform ${pop ? "rotate-90" : ""}`}>›</span>
      </button>
      {pop && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label="รายละเอียดเหตุการณ์ความเสี่ยง"
          style={{ position: "fixed", top: pos.top, left: pos.left, width: "min(360px, calc(100vw - 16px))" }}
          className="z-50 max-h-[80vh] overflow-y-auto rounded-xl border border-slate-600 bg-slate-900/95 backdrop-blur px-3.5 py-3 text-xs leading-relaxed text-slate-100 shadow-xl"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold text-slate-50">{riskEventLabel(ev.event_type)}</span>
            <span className={`text-[11px] px-1.5 py-0.5 rounded ${riskEventTone(ev.event_type)}`}>
              {ev.event_type}
            </span>
          </div>
          <p className="mt-0.5 text-[11px] text-slate-300">{auditTime(ev.created_at)}</p>

          {(before !== null || after !== null) && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <p className="text-slate-50">
                ขยายลิมิต <b>{before !== null ? `${fmtNum(before, 1)}%` : "—"}</b>
                <span className="text-slate-500"> → </span>
                <b>{after !== null ? `${fmtNum(after, 1)}%` : "—"}</b>
                {d.approved === false && <span className="text-slate-400"> (ไม่ขยาย — คงลิมิตเดิม)</span>}
              </p>
              <p className="text-[11px] mt-1 text-slate-300">
                ผลคือไม้เปิดทั้งหมดยังอยู่และทำงานต่อ (SL/TP ทำงานปกติ) — policy
                ปัจจุบันคือ &quot;เขียน DB ไม่สำเร็จ/ไม่ได้รับอนุมัติ ⇒ ห้ามปิดไม้&quot;
              </p>
            </>
          )}

          {level && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <p className="text-slate-50">
                ระดับความเสี่ยง <b className="text-rose-300">{level}</b>
                {typeof d.trading_paused === "boolean" && (
                  <span className="text-slate-400"> · {d.trading_paused ? "หยุดเทรด" : "เทรดต่อ"}</span>
                )}
              </p>
            </>
          )}

          {triggers.length > 0 && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400 mb-1">
                เงื่อนไขที่ทำให้ต้องขอขยาย ({triggers.length})
              </p>
              <ul className="space-y-1">
                {triggers.map((t, i) => {
                  const o = (t && typeof t === "object") ? (t as Record<string, unknown>) : { value: t };
                  return (
                    <li key={i} className="flex gap-1.5">
                      <span className="text-slate-500 shrink-0">•</span>
                      <span className="break-all">{kvLine(o) || String(t)}</span>
                    </li>
                  );
                })}
              </ul>
            </>
          )}

          {breaches.length > 0 && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400 mb-1">
                ลิมิตที่ถูกแตะ ({breaches.length})
              </p>
              <ul className="space-y-1">
                {breaches.map((b, i) => (
                  <li key={i} className="flex gap-1.5">
                    <span className="text-slate-500 shrink-0">•</span>
                    <span className="break-all">{b}</span>
                  </li>
                ))}
              </ul>
            </>
          )}

          {message && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <p className="text-[11px] text-slate-300">{message}</p>
            </>
          )}

          <div className="border-t border-slate-700 my-2" />
          <p className="text-[10px] font-mono break-all text-slate-400" title="ค่าดิบจาก risk_events.detail">
            {Object.keys(d).length > 0 ? JSON.stringify(d) : "ไม่มีรายละเอียด"}
          </p>
        </div>,
        document.body
      )}
    </div>
  );
}

/**
 * ข้อความ empty-state ของตาราง scheduler_runs (scheduler / guard tab)
 *
 * เดิมขึ้น "ต้องรัน migration 030_scheduler_logs.sql" เสมอ ซึ่งทำให้เข้าใจผิด:
 * ตารางอาจมีอยู่และทำงานปกติ แต่ job ยังไม่ "เสร็จแล้วบันทึก log" — prod
 * 2026-09-11: position_guard ใช้เวลา ~55 วิ/รอบ เกิน interval 1 นาที จึงถูก
 * ข้ามด้วย max_instances=1 ตลอด → ไม่มี row ทั้งที่ตารางปกติดี
 *
 * แยก 2 กรณีด้วย summary.total ของทุก job: ถ้ามี log ของ job อื่นอยู่ =
 * ตารางทำงาน (ไม่ใช่ปัญหา migration) → บอกให้รอ/กดรันเดี๋ยวนี้
 */
function schedulerEmptyMessage(
  jobLabel: string,
  summary: SchedulerLogsResponse["summary"] | null,
) {
  if ((summary?.total ?? 0) > 0) {
    return `ยังไม่มีรอบ ${jobLabel} ที่บันทึกได้ — ตาราง scheduler_runs ทำงานปกติ (job อื่นมี log) แต่รอบนี้ยังไม่เสร็จหรือถูกข้าม รอรอบถัดไป (~1 นาที)`;
  }
  return "ยังไม่มีประวัติ scheduler เลย — ตรวจสอบว่ารัน migration 030_scheduler_logs.sql บน Supabase แล้ว และ backend ตั้ง ENABLE_WORKERS=1";
}

function BucketCard({ label, bucket }: { label: string; bucket?: { total: number; success: number; error: number } | null }) {
  if (!bucket) return null;
  return (
    <div className="panel">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="text-2xl font-bold">{bucket.total.toLocaleString()}</p>
      <p className="text-xs mt-1">
        <span className="text-emerald-400">✓ {bucket.success}</span>
        {"  "}
        <span className="text-red-400">✗ {bucket.error}</span>
      </p>
    </div>
  );
}

export default function LogsPage() {
  const [tab, setTab] = useState<"quotes" | "news" | "scheduler" | "guard" | "gate" | "audit">("quotes");
  const [logs, setLogs] = useState<QuoteApiLog[]>([]);
  const [summary, setSummary] = useState<QuoteLogSummary | null>(null);
  const [newsLogs, setNewsLogs] = useState<NewsLog[]>([]);
  const [newsSummary, setNewsSummary] = useState<NewsLogsResponse["summary"] | null>(null);
  const [schedLogs, setSchedLogs] = useState<SchedulerLog[]>([]);
  const [schedSummary, setSchedSummary] = useState<SchedulerLogsResponse["summary"] | null>(null);
  const [ttlDays, setTtlDays] = useState(7);
  const [err, setErr] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");
  const [loading, setLoading] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<QuoteTestResult | null>(null);
  const [filter, setFilter] = useState<"all" | "forex" | "gold">("all");
  const [page, setPage] = useState(1);
  // refs ให้ callbacks เสถียร (ไม่ต้องใส่ tab/filter ใน deps → ไม่โหลดซ้ำวน)
  const tabRef = useRef<"quotes" | "news" | "scheduler" | "guard" | "gate" | "audit">("quotes");
  const filterRef = useRef<"all" | "forex" | "gold">("all");
  // server paging: ขอทีละหน้า (500 แถว/ครั้ง) แล้วแบ่งแสดง 50/หน้า —
  // ตาราง 7 วันโตเกิน 500 ได้ จึงต้องเดิน offset ไปเรื่อย ๆ ไม่ใช่ดึงแค่ 500 ล่าสุด
  const [quoteTotal, setQuoteTotal] = useState(0);
  const [quoteHasMore, setQuoteHasMore] = useState(false);
  const [newsTotal, setNewsTotal] = useState(0);
  const [newsHasMore, setNewsHasMore] = useState(false);
  const [schedTotal, setSchedTotal] = useState(0);
  const [schedHasMore, setSchedHasMore] = useState(false);
  // แท็บ Guard = scheduler_runs ที่ job_id=position_guard (server filter)
  const [guardLogs, setGuardLogs] = useState<SchedulerLog[]>([]);
  const [guardTotal, setGuardTotal] = useState(0);
  const [guardHasMore, setGuardHasMore] = useState(false);
  // heartbeat ของ scheduler ในโปรเซส (app.main._JOB_STATS ผ่าน /scheduler-logs)
  // — พิสูจน์ว่า job "ถูกเรียก" ต่างจาก row ที่อาจถูกข้าม/ไม่ถูกเขียน
  const [jobStats, setJobStats] = useState<Record<string, SchedulerJobStat> | null>(null);
  // แท็บ Gate = signal_logs ฝั่ง execution gate (order_blocked = gate ปัดตก,
  // order_opened = gate ผ่าน) — server filter ด้วย event
  const [gateLogs, setGateLogs] = useState<SignalLog[]>([]);
  const [gateSummary, setGateSummary] = useState<SignalLogsResponse["summary"] | null>(null);
  const [gateTotal, setGateTotal] = useState(0);
  const [gateHasMore, setGateHasMore] = useState(false);
  const [gateFilter, setGateFilter] = useState<"order_blocked" | "order_opened" | "all">("all");
  const gateFilterRef = useRef<"order_blocked" | "order_opened" | "all">("all");
  // แท็บ Audit = risk_events (ประวัติการตัดสินใจความเสี่ยง — ไม่มี TTL)
  // คู่กับ kill_expand_requests (คำขอยืนยันขยายลิมิตที่เจ้าของกด/ระบบขยายให้)
  const [auditLogs, setAuditLogs] = useState<RiskEventLog[]>([]);
  const [auditReqs, setAuditReqs] = useState<RiskAuditRequest[]>([]);
  const [auditSummary, setAuditSummary] = useState<RiskLogsResponse["summary"] | null>(null);
  const [auditHint, setAuditHint] = useState("");
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditHasMore, setAuditHasMore] = useState(false);
  type RiskFilter = "all" | "limit_breach" | "limit_expanded" | "limit_expand_rejected";
  const [auditFilter, setAuditFilter] = useState<RiskFilter>("all");
  const auditFilterRef = useRef<RiskFilter>("all");
  const PAGE_SIZE = 50;
  const SERVER_PAGE = 500;

  const loadQuotes = useCallback(async (flt: "all" | "forex" | "gold", pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    const qres = await api.quoteLogs(SERVER_PAGE, offset, flt);
    return qres;
  }, []);

  const loadNews = useCallback(async (pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.newsLogs(SERVER_PAGE, offset);
  }, []);

  const loadSched = useCallback(async (pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.schedulerLogs(SERVER_PAGE, offset);
  }, []);

  const loadGuard = useCallback(async (pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.schedulerLogs(SERVER_PAGE, offset, "position_guard");
  }, []);

  const loadGate = useCallback(async (pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.signalLogs(SERVER_PAGE, offset, gateFilterRef.current);
  }, []);

  const loadAudit = useCallback(async (pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.riskLogs(SERVER_PAGE, offset, auditFilterRef.current);
  }, []);

  // โหลดเฉพาะแท็บที่เปิดอยู่ (lazy) — เข้าหน้าครั้งแรกยิงแค่ 1 request
  // แทน 5 requests พร้อมกัน (quotes+news+scheduler+guard+gate) ทำให้หน้าแรกไวขึ้น ~5 เท่า
  const loadedRef = useRef<Set<string>>(new Set());
  const loadOne = useCallback(async (t: "quotes" | "news" | "scheduler" | "guard" | "gate" | "audit", flt?: "all" | "forex" | "gold") => {
    setLoading(true);
    try {
      if (t === "quotes") {
        const f = flt ?? filterRef.current;
        const qres = await loadQuotes(f, 1);
        setLogs(qres.logs ?? []);
        setSummary(qres.summary ?? null);
        setTtlDays(qres.ttl_days ?? 7);
        setQuoteTotal(qres.total ?? (qres.logs ?? []).length);
        setQuoteHasMore(qres.has_more ?? false);
      } else if (t === "news") {
        const nres = await loadNews(1);
        setNewsLogs(nres.logs ?? []);
        setNewsSummary(nres.summary ?? null);
        setNewsTotal(nres.total ?? (nres.logs ?? []).length);
        setNewsHasMore(nres.has_more ?? false);
      } else if (t === "guard") {
        const gres = await loadGuard(1);
        setGuardLogs(gres.logs ?? []);
        setGuardTotal(gres.total ?? (gres.logs ?? []).length);
        setGuardHasMore(gres.has_more ?? false);
        setJobStats(gres.job_stats ?? null);
        // summary guard อาศัย schedSummary — โหลด scheduler summary แบบเบา (limit 1) ครั้งเดียว
        if (!loadedRef.current.has("scheduler")) {
          try {
            const sres = await api.schedulerLogs(1, 0);
            setSchedSummary(sres.summary ?? null);
          } catch { /* ignore */ }
        }
      } else if (t === "gate") {
        const gates = await loadGate(1);
        setGateLogs(gates.logs ?? []);
        setGateSummary(gates.summary ?? null);
        setGateTotal(gates.total ?? (gates.logs ?? []).length);
        setGateHasMore(gates.has_more ?? false);
      } else if (t === "audit") {
        const ares = await loadAudit(1);
        setAuditLogs(ares.logs ?? []);
        setAuditReqs(ares.requests ?? []);
        setAuditSummary(ares.summary ?? null);
        setAuditHint(ares.audit_hint ?? "");
        setAuditTotal(ares.summary?.total ?? (ares.logs ?? []).length);
        setAuditHasMore(ares.has_more ?? false);
      } else {
        const sres = await loadSched(1);
        setSchedLogs(sres.logs ?? []);
        setSchedSummary(sres.summary ?? null);
        setSchedTotal(sres.total ?? (sres.logs ?? []).length);
        setSchedHasMore(sres.has_more ?? false);
      }
      loadedRef.current.add(t);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
      setPage(1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [loadAudit, loadGate, loadGuard, loadNews, loadQuotes, loadSched]);

  // เปลี่ยน server page (ทุก 10 หน้า UI = 500 แถว) — ดึง chunk ถัดไปจาก backend
  const gotoServerPage = useCallback(async (pg: number) => {
    setLoading(true);
    try {
      if (tabRef.current === "quotes") {
        const qres = await loadQuotes(filterRef.current, pg);
        setLogs(qres.logs ?? []);
        setQuoteTotal(qres.total ?? 0);
        setQuoteHasMore(qres.has_more ?? false);
      } else if (tabRef.current === "news") {
        const nres = await loadNews(pg);
        setNewsLogs(nres.logs ?? []);
        setNewsTotal(nres.total ?? 0);
        setNewsHasMore(nres.has_more ?? false);
      } else if (tabRef.current === "guard") {
        const gres = await loadGuard(pg);
        setGuardLogs(gres.logs ?? []);
        setGuardTotal(gres.total ?? 0);
        setGuardHasMore(gres.has_more ?? false);
        setJobStats(gres.job_stats ?? null);
      } else if (tabRef.current === "gate") {
        const gates = await loadGate(pg);
        setGateLogs(gates.logs ?? []);
        setGateTotal(gates.total ?? 0);
        setGateHasMore(gates.has_more ?? false);
      } else if (tabRef.current === "audit") {
        const ares = await loadAudit(pg);
        setAuditLogs(ares.logs ?? []);
        setAuditReqs(ares.requests ?? []);
        setAuditTotal(ares.summary?.total ?? (ares.logs ?? []).length);
        setAuditHasMore(ares.has_more ?? false);
      } else {
        const sres = await loadSched(pg);
        setSchedLogs(sres.logs ?? []);
        setSchedTotal(sres.total ?? 0);
        setSchedHasMore(sres.has_more ?? false);
      }
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
      setPage(1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [loadAudit, loadGate, loadGuard, loadNews, loadQuotes, loadSched]);

  useEffect(() => {
    loadOne("quotes");
  }, [loadOne]);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api.quoteTest();
      setTestResult(r);
      // ทดสอบสร้าง quote log ใหม่ — ล้างแคช quotes แล้วโหลดแท็บปัจจุบัน
      // (ถ้าอยู่แท็บอื่น quotes จะโหลดใหม่ตอนกดเข้ามา)
      loadedRef.current.delete("quotes");
      await loadOne(tabRef.current);
    } catch (e) {
      setTestResult({
        verdict: "fail",
        prices: {},
        failures: {},
        error: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setTesting(false);
    }
  };

  // ตารางคือ server page ปัจจุบัน (500 แถว) — filter ราคาทำฝั่ง server แล้ว
  const shown = logs;
  // จำนวนหน้าทั้งหมดอ้างจาก total จริง (count=exact) ไม่ใช่ความยาว chunk
  const totalPages = Math.max(1, Math.ceil((quoteTotal || shown.length) / PAGE_SIZE));
  const uiPagesPerChunk = Math.max(1, Math.ceil(SERVER_PAGE / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const chunkPage = ((safePage - 1) % uiPagesPerChunk) + 1;
  const pageRows = shown.slice((chunkPage - 1) * PAGE_SIZE, chunkPage * PAGE_SIZE);
  const newsChunkTotal = newsLogs.length;
  const newsTotalPages = Math.max(1, Math.ceil((newsTotal || newsChunkTotal) / PAGE_SIZE));
  const newsSafePage = Math.min(page, newsTotalPages);
  const newsChunkPage = ((newsSafePage - 1) % uiPagesPerChunk) + 1;
  const newsPageRows = newsLogs.slice((newsChunkPage - 1) * PAGE_SIZE, newsChunkPage * PAGE_SIZE);
  const schedChunkTotal = schedLogs.length;
  const schedTotalPages = Math.max(1, Math.ceil((schedTotal || schedChunkTotal) / PAGE_SIZE));
  const schedSafePage = Math.min(page, schedTotalPages);
  const schedChunkPage = ((schedSafePage - 1) % uiPagesPerChunk) + 1;
  const schedPageRows = schedLogs.slice((schedChunkPage - 1) * PAGE_SIZE, schedChunkPage * PAGE_SIZE);
  // แท็บ Guard ใช้ chunk ตัวเอง (server filter job=position_guard แล้ว)
  const guardChunkTotal = guardLogs.length;
  const guardTotalPages = Math.max(1, Math.ceil((guardTotal || guardChunkTotal) / PAGE_SIZE));
  const guardSafePage = Math.min(page, guardTotalPages);
  const guardChunkPage = ((guardSafePage - 1) % uiPagesPerChunk) + 1;
  const guardPageRows = guardLogs.slice((guardChunkPage - 1) * PAGE_SIZE, guardChunkPage * PAGE_SIZE);
  const guardServerPage = Math.floor((guardSafePage - 1) / uiPagesPerChunk) + 1;
  const guardServerPages = Math.max(1, Math.ceil(guardTotalPages / uiPagesPerChunk));
  // แท็บ Gate ใช้ chunk ตัวเอง (server filter event แล้ว)
  const gateChunkTotal = gateLogs.length;
  const gateTotalPages = Math.max(1, Math.ceil((gateTotal || gateChunkTotal) / PAGE_SIZE));
  const gateSafePage = Math.min(page, gateTotalPages);
  const gateChunkPage = ((gateSafePage - 1) % uiPagesPerChunk) + 1;
  const gatePageRows = gateLogs.slice((gateChunkPage - 1) * PAGE_SIZE, gateChunkPage * PAGE_SIZE);
  const gateServerPage = Math.floor((gateSafePage - 1) / uiPagesPerChunk) + 1;
  const gateServerPages = Math.max(1, Math.ceil(gateTotalPages / uiPagesPerChunk));
  // แท็บ Audit ใช้ chunk ตัวเอง (server filter event_type แล้ว)
  const auditChunkTotal = auditLogs.length;
  const auditTotalPages = Math.max(1, Math.ceil((auditTotal || auditChunkTotal) / PAGE_SIZE));
  const auditSafePage = Math.min(page, auditTotalPages);
  const auditChunkPage = ((auditSafePage - 1) % uiPagesPerChunk) + 1;
  const auditPageRows = auditLogs.slice((auditChunkPage - 1) * PAGE_SIZE, auditChunkPage * PAGE_SIZE);
  const auditServerPage = Math.floor((auditSafePage - 1) / uiPagesPerChunk) + 1;
  const auditServerPages = Math.max(1, Math.ceil(auditTotalPages / uiPagesPerChunk));
  const serverPage = Math.floor((safePage - 1) / uiPagesPerChunk) + 1;
  const newsServerPage = Math.floor((newsSafePage - 1) / uiPagesPerChunk) + 1;
  const schedServerPage = Math.floor((schedSafePage - 1) / uiPagesPerChunk) + 1;
  const serverPages = Math.max(1, Math.ceil(totalPages / uiPagesPerChunk));
  const newsServerPages = Math.max(1, Math.ceil(newsTotalPages / uiPagesPerChunk));
  const schedServerPages = Math.max(1, Math.ceil(schedTotalPages / uiPagesPerChunk));

  return (
    <div className="space-y-4">
      {/* ---------- Header + test button ---------- */}
      <section className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold flex items-center gap-2"><Icon n="scroll" size={19} /> Logs</h2>
          <p className="text-xs text-slate-500">
            {tab === "quotes"
              ? `บันทึกการดึงราคาทุกครั้งจาก API ภายนอก — เก็บ ${ttlDays} วัน ลบเกินอายุอัตโนมัติ`
              : tab === "news"
                ? "ประวัติการวิเคราะห์ข่าว (worker ทุก 15 นาที) — พาดหัวจริง + sentiment จาก AI"
                : tab === "guard"
                  ? "การทำงานของ Guard ทุกรอบ (ทุก 1 นาที) — เช็ค SL/TP, ขยับ SL, ปิดไม้ (เก็บ 7 วัน)"
                  : tab === "gate"
                    ? "Gate อนุมัติ/ปัดตกออเดอร์ — เหตุผลทุกครั้งที่เปิดหรือบล็อก (เก็บ 7 วัน)"
                    : tab === "audit"
                      ? "ประวัติเหตุการณ์ความเสี่ยง — kill switch เข้าเงื่อนไข · เจ้าของอนุมัติ/ปฏิเสธขยายลิมิต (เก็บถาวร ไม่มีอายุ)"
                      : "ประวัติการทำงาน scheduler ทุก job — พิสูจน์ว่า worker ยังรันอยู่ (เก็บ 7 วัน)"}
            {updatedAt && ` · อัปเดต ${updatedAt}`}
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => loadOne(tabRef.current)} disabled={loading}
            aria-busy={loading} aria-live="polite"
            title={loading ? "กำลังโหลดข้อมูล..." : "รีเฟรชข้อมูลตอนนี้"}
            className="btn-secondary disabled:opacity-50">
            <span className="inline-flex items-center gap-1.5">
              {loading && <Icon n="spinner" size={14} className="animate-spin" />}
              รีเฟรช
            </span>
          </button>
          <button onClick={runTest} disabled={testing} className="btn-primary">
            <span className="inline-flex items-center gap-1.5">
              {testing && <Icon n="spinner" size={14} className="animate-spin" />}
              {testing ? "กำลังทดสอบ..." : "ทดสอบดึงราคา"}
            </span>
          </button>
        </div>
      </section>

      {/* ---------- Tabs: quotes / news / scheduler ---------- */}
      <div className="flex gap-2 text-xs">
        <button
          onClick={() => { setTab("quotes"); tabRef.current = "quotes"; setPage(1); if (!loadedRef.current.has("quotes")) loadOne("quotes"); }}
          className={`px-3 py-1 rounded ${tab === "quotes" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          ราคา (Quote API)
        </button>
        <button
          onClick={() => { setTab("news"); tabRef.current = "news"; setPage(1); if (!loadedRef.current.has("news")) loadOne("news"); }}
          className={`px-3 py-1 rounded ${tab === "news" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          ข่าว (News{newsSummary ? ` ${newsSummary.total}` : ""})
        </button>
        <button
          onClick={() => { setTab("scheduler"); tabRef.current = "scheduler"; setPage(1); if (!loadedRef.current.has("scheduler")) loadOne("scheduler"); }}
          className={`px-3 py-1 rounded ${tab === "scheduler" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          Scheduler{schedSummary ? ` ${schedSummary.total}` : ""}
        </button>
        <button
          onClick={() => { setTab("guard"); tabRef.current = "guard"; setPage(1); if (!loadedRef.current.has("guard")) loadOne("guard"); }}
          className={`px-3 py-1 rounded ${tab === "guard" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          Guard{schedSummary?.by_job?.position_guard ? ` ${schedSummary.by_job.position_guard.total}` : guardTotal ? ` ${guardTotal}` : ""}
        </button>
        <button
          onClick={() => { setTab("gate"); tabRef.current = "gate"; setPage(1); if (!loadedRef.current.has("gate")) loadOne("gate"); }}
          className={`px-3 py-1 rounded ${tab === "gate" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          Gate{gateSummary ? ` ${gateSummary.blocked + gateSummary.opened}` : gateTotal ? ` ${gateTotal}` : ""}
        </button>
        <button
          onClick={() => { setTab("audit"); tabRef.current = "audit"; setPage(1); if (!loadedRef.current.has("audit")) loadOne("audit"); }}
          title="ประวัติเหตุการณ์ความเสี่ยง (kill switch / ขยายลิมิต) — เก็บถาวร"
          className={`px-3 py-1 rounded ${tab === "audit" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          Audit{auditTotal > 0 ? ` ${auditTotal}` : ""}
        </button>
      </div>

      {err && <p className="text-loss text-sm">{err}</p>}

      {/* ---------- Test result ---------- */}
      {testResult && (
        <section className={`panel ${testResult.verdict === "ok" ? "border-profit" : "border-loss"}`}>
          <p className="text-sm font-bold mb-2 flex items-center gap-1.5">
            {testResult.verdict === "ok"
              ? <><Icon n="checkCircle" size={15} className="text-profit" /> ทดสอบสำเร็จ</>
              : <><Icon n="xCircle" size={15} className="text-loss" /> ทดสอบล้มเหลว</>}
            {testResult.hint && <span className="text-xs font-normal text-slate-400"> — {testResult.hint}</span>}
          </p>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-sm">
            {Object.entries(testResult.prices).map(([asset, price]) => (
              <div key={asset} className="bg-slate-800/60 rounded px-3 py-2">
                <p className="text-xs text-slate-500">{asset}</p>
                <p className="font-bold text-profit">{fmtNum(price, 4)}</p>
              </div>
            ))}
            {Object.entries(testResult.failures).map(([asset, msg]) => (
              <div key={asset} className="bg-slate-800/60 rounded px-3 py-2">
                <p className="text-xs text-slate-500">{asset}</p>
                <p className="text-xs text-loss truncate" title={msg}>{msg}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ---------- Summary card: forex vs gold ---------- */}
      {tab === "quotes" && (
      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <BucketCard label="ยิงทั้งหมด (7 วัน)" bucket={summary} />
        <BucketCard label="Forex" bucket={summary?.forex} />
        <BucketCard label="Gold" bucket={summary?.gold} />
        <div className="panel">
          <p className="text-xs text-slate-500">สถานะรวม</p>
          <p className="text-2xl font-bold">
            <span className="text-emerald-400">{summary?.success ?? 0}</span>
            <span className="text-slate-500 text-base"> / </span>
            <span className="text-red-400">{summary?.error ?? 0}</span>
          </p>
          <p className="text-xs text-slate-500 mt-1">success / error</p>
        </div>
      </section>
      )}

      {/* ---------- News summary ---------- */}
      {tab === "news" && newsSummary && (
      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="panel">
          <p className="text-xs text-slate-500">วิเคราะห์ทั้งหมด</p>
          <p className="text-2xl font-bold">{newsSummary.total}</p>
          <p className="text-xs mt-1">
            <span className="text-emerald-400">พาดหัวจริง {newsSummary.real}</span>
            {"  "}
            <span className="text-amber-400">heuristic {newsSummary.heuristic}</span>
          </p>
        </div>
        {Object.entries(newsSummary.by_event).map(([ev, n]) => (
          <div key={ev} className="panel">
            <p className="text-xs text-slate-500">{ev}</p>
            <p className="text-2xl font-bold">{n}</p>
          </div>
        ))}
      </section>
      )}

      {/* ---------- Scheduler summary ---------- */}
      {tab === "scheduler" && schedSummary && (
      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="panel">
          <p className="text-xs text-slate-500">รันทั้งหมด (7 วัน)</p>
          <p className="text-2xl font-bold">{schedSummary.total}</p>
          <p className="text-xs mt-1">
            <span className="text-emerald-400">ok {schedSummary.ok}</span>
            {"  "}
            <span className="text-red-400">error {schedSummary.error}</span>
          </p>
        </div>
        {Object.entries(schedSummary.by_job).map(([job, b]) => (
          <div key={job} className="panel">
            <p className="text-xs text-slate-500">{job}</p>
            <p className="text-2xl font-bold">{b.total}</p>
            <p className="text-xs mt-1">
              <span className="text-emerald-400">ok {b.ok}</span>
              {b.error > 0 && <span className="text-red-400"> ✗{b.error}</span>}
            </p>
          </div>
        ))}
      </section>
      )}

      {/* ---------- Guard summary (position_guard เท่านั้น) ---------- */}
      {tab === "guard" && (() => {
        const b = schedSummary?.by_job?.position_guard;
        const latest = guardLogs[0];
        const latestKv = parseGuardDetail(latest?.detail ?? "");
        // map ของ "key -> ข้อความดิบ" สำหรับ list ชื่อคู่เงิน (parseGuardDetail
        // ให้แต่ตัวเลข จึงต้องใช้ map ดิบสำหรับ sl_assets / closed_assets)
        const latestRaw = splitGuardDetail(latest?.detail ?? "");
        let moved500 = 0;
        let closed500 = 0;
        let skipped500 = 0;
        for (const g of guardLogs) {
          const kv = parseGuardDetail(g.detail ?? "");
          moved500 += kv.moved_sl ?? 0;
          closed500 += kv.closed ?? 0;
          skipped500 += kv.skipped_prev_running ?? 0;
        }
        // heartbeat จาก scheduler ในโปรเซส (ไม่ใช่ row) — อันนี้คือหลักฐานว่า
        // job "ถูกเรียก" จริง ก่อนหน้านี้ APScheduler ข้ามรอบแบบเงียบ (misfire/
        // max_instances) ทำให้ไม่มี row เลย และหน้า Guard ว่างทั้งที่ guard ทำงานอยู่
        const hb = jobStats?.position_guard;
        const lagS = hb?.last_started ? Math.max(0, Math.round(Date.now() / 1000 - hb.last_started)) : null;
        const heartbeatProblem =
          !!hb && hb.running_s !== null && hb.running_s !== undefined;
        const tickRowMismatch = !!hb && !!b && hb.ticks > 0 && b.total < hb.ticks - 1;
        return (
        <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="panel">
            <p className="text-xs text-slate-500">รอบ guard (7 วัน)</p>
            <p className="text-2xl font-bold">{b ? b.total.toLocaleString() : guardTotal.toLocaleString()}</p>
            <p className="text-xs mt-1">
              {b ? (<><span className="text-emerald-400">ok {b.ok}</span>{"  "}<span className="text-red-400">error {b.error}</span></>) : (<span className="text-slate-500">ทุก 1 นาที/รอบ</span>)}
              {skipped500 > 0 && <span className="text-amber-400">{"  "}ข้าม {skipped500}</span>}
            </p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">รอบล่าสุด</p>
            <p className="text-sm font-bold mt-1">{latest?.created_at ? new Date(latest.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}</p>
            <p className="text-xs mt-1 text-slate-400">
              {latestKv.skipped_prev_running
                ? "รอบนี้ถูกข้าม — รอบก่อนยังไม่จบ (ดู heartbeat ด้านล่าง)"
                : `ตรวจ ${latestKv.checked ?? "—"} ไม้ · ขยับ SL ${latestKv.moved_sl ?? 0} · ปิด ${latestKv.closed ?? 0}`}
            </p>
            {!latestKv.skipped_prev_running && (latestRaw.sl_assets || latestRaw.closed_assets) && (
              <p className="text-[11px] mt-1 text-slate-500 break-words">
                {latestRaw.sl_assets && (
                  <span className="text-amber-200/80">
                    ขยับ: {guardChipItems(latestRaw.sl_assets).map(guardSlLabel).join(" · ")}
                  </span>
                )}
                {latestRaw.sl_assets && latestRaw.closed_assets && " · "}
                {latestRaw.closed_assets && (
                  <span className="text-sky-200/80">
                    ปิด: {guardChipItems(latestRaw.closed_assets).map(guardClosedLabel).join(" · ")}
                  </span>
                )}
              </p>
            )}
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">ขยับ SL (500 รอบล่าสุด)</p>
            <p className="text-2xl font-bold text-amber-400">{moved500}</p>
            <p className="text-xs text-slate-500 mt-1">ครั้งที่ guard เลื่อน stop กระชับขึ้น</p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">ปิดไม้ (500 รอบล่าสุด)</p>
            <p className="text-2xl font-bold">{closed500}</p>
            <p className="text-xs text-slate-500 mt-1">SL/TP + smart + time-stop</p>
          </div>
          {hb && (
            <div className="panel col-span-2 md:col-span-4">
              <p className="text-xs text-slate-500">Heartbeat ของ scheduler (ในโปรเซส — ไม่ใช่ row ใน DB)</p>
              <p className="text-xs mt-1 text-slate-300">
                เรียก guard {hb.ticks} ครั้ง · ok {hb.ok} · error {hb.error} · ข้าม {hb.skipped}
                {lagS !== null && ` · รอบล่าสุด ${lagS} วินาทีที่แล้ว`}
                {hb.last_ms !== null && hb.last_ms !== undefined && ` · ใช้ ${hb.last_ms} ms`}
                {hb.last_status && ` · สถานะ ${hb.last_status}`}
              </p>
              {heartbeatProblem && (
                <p className="text-xs mt-1 text-amber-400">
                  รอบปัจจุบันยังรันอยู่ {hb.running_s} วินาที — ถ้าค้างนานกว่านี้ รอบถัดไปจะถูกบันทึกเป็น &quot;ข้าม&quot; (ยังเห็นเป็น row ไม่หายไปแล้ว)
                </p>
              )}
              {tickRowMismatch && (
                <p className="text-xs mt-1 text-amber-400">
                  scheduler เรียกรวม {hb.ticks} ครั้ง แต่ใน 7 วันมีแค่ {b?.total ?? 0} row — ส่วนต่างคือรอบที่ถูกข้าม/เขียน log ไม่สำเร็จ
                </p>
              )}
              {hb.last_error && (
                <p className="text-xs mt-1 text-red-400">error ล่าสุด: {hb.last_error}</p>
              )}
            </div>
          )}
        </section>
        );
      })()}

      {/* ---------- Gate summary (order_blocked vs order_opened) ---------- */}
      {tab === "gate" && gateSummary && (
      <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div className="panel">
          <p className="text-xs text-slate-500">Gate ปัดตก (7 วัน)</p>
          <p className="text-2xl font-bold text-amber-400">{gateSummary.blocked.toLocaleString()}</p>
          <p className="text-xs text-slate-500 mt-1">order_blocked — pause/limit/news/corr/heat</p>
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">Gate ผ่าน (7 วัน)</p>
          <p className="text-2xl font-bold text-emerald-400">{gateSummary.opened.toLocaleString()}</p>
          <p className="text-xs text-slate-500 mt-1">order_opened — เปิดออเดอร์จริง (ไม่รวมการขยับ SL แล้ว)</p>
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">ย้าย SL (7 วัน)</p>
          <p className="text-2xl font-bold text-cyan-300">{(gateSummary.sl_moved ?? 0).toLocaleString()}</p>
          <p className="text-xs text-slate-500 mt-1">sl_moved — breakeven / trailing / ปรับมือ</p>
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">หมดอายุ (7 วัน)</p>
          <p className="text-2xl font-bold">{gateSummary.expired.toLocaleString()}</p>
          <p className="text-xs text-slate-500 mt-1">expired — pending เกิน 30 นาที</p>
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">ปิดไม้ (7 วัน)</p>
          <p className="text-2xl font-bold">{gateSummary.closed.toLocaleString()}</p>
          <p className="text-xs text-slate-500 mt-1">closed — SL/TP/manual</p>
        </div>
      </section>
      )}

      {/* ---------- Provider breakdown ---------- */}
      {tab === "quotes" && summary && Object.keys(summary.by_provider).length > 0 && (
        <section className="panel">
          <p className="text-xs text-slate-500 mb-2">แยกตาม Provider</p>
          <div className="flex flex-wrap gap-2 text-xs">
            {Object.entries(summary.by_provider).map(([prov, b]) => (
              <span key={prov} className="bg-slate-800/60 rounded px-3 py-1">
                <b>{prov}</b>: {b.total} calls
                <span className="text-emerald-400"> ✓{b.success}</span>
                {b.error > 0 && <span className="text-red-400"> ✗{b.error}</span>}
              </span>
            ))}
          </div>
        </section>
      )}

      {/* ---------- Filter + log table ---------- */}
      {/* หมายเหตุ: overflow-x-auto ต้องอยู่ div ลูก ไม่ใช่บน .panel — backdrop-filter
          บนตัว scroll container เองจะพังใน Chromium (blur หายเมื่อตารางโหลด/เลื่อน) */}
      {tab === "quotes" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <div className="flex gap-2 mb-3 text-xs">
          {(["all", "forex", "gold"] as const).map((f) => (
            <button
              key={f}
              onClick={() => { setFilter(f); filterRef.current = f; setPage(1); loadOne("quotes", f); }}
              className={`px-3 py-1 rounded ${filter === f ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}
            >
              {f === "all" ? "ทั้งหมด" : f === "forex" ? "Forex" : "Gold"}
            </button>
          ))}
        </div>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">Asset</th>
              <th className="py-2 pr-3">ประเภท</th>
              <th className="py-2 pr-3">Provider</th>
              <th className="py-2 pr-3">URL</th>
              <th className="py-2 pr-3">API Key</th>
              <th className="py-2 pr-3">สถานะ</th>
              <th className="py-2 pr-3">HTTP</th>
              <th className="py-2 pr-3">ราคา</th>
              <th className="py-2 pr-3">ms</th>
              <th className="py-2">Error</th>
            </tr>
          </thead>
          <tbody>
            {loading && logs.length === 0 && (
              <tr><td colSpan={11} className="py-6">
                <LoadingGraphic message="กำลังโหลดบันทึก API — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && shown.length === 0 && (
              <tr><td colSpan={11} className="py-6 text-center text-slate-500">
                ยังไม่มี log — กด &quot;ทดสอบดึงราคา&quot; เพื่อสร้างรายการแรก
              </td></tr>
            )}
            {pageRows.map((l) => (
              <tr key={l.id} className="border-b border-slate-800/50 hover:bg-white/[0.04]">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {new Date(l.created_at).toLocaleString("th-TH", { hour12: false })}
                </td>
                <td className="py-2 pr-3 font-bold">{l.asset}</td>
                <td className="py-2 pr-3">
                  <span className={l.category === "gold" ? "text-amber-400" : "text-sky-400"}>
                    {l.category}
                  </span>
                </td>
                <td className="py-2 pr-3">{l.provider}</td>
                <td className="py-2 pr-3 max-w-[260px] truncate text-slate-400" title={l.url}>{l.url}</td>
                <td className="py-2 pr-3 font-mono text-slate-500">{l.api_key_hint || "—"}</td>
                <td className="py-2 pr-3">
                  <span className={`px-2 py-0.5 rounded ${statusBadge(l.status)}`}>{l.status}</span>
                </td>
                <td className="py-2 pr-3">{l.http_status ?? "—"}</td>
                <td className="py-2 pr-3 font-mono">{l.price != null ? fmtNum(l.price, 4) : "—"}</td>
                <td className="py-2 pr-3 text-slate-400">{l.duration_ms ?? "—"}</td>
                <td className="py-2 max-w-[200px] truncate" title={l.error ?? ""}><span className="text-loss">{l.error || "—"}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        {(logs.length > 0 || quoteTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {safePage}/{totalPages} · แสดง {pageRows.length} จาก {quoteTotal.toLocaleString()} รายการ
              {serverPages > 1 && ` · ชุดที่ ${serverPage}/${serverPages}`} · เก็บสูงสุด {ttlDays} วัน
            </p>
            {totalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (chunkPage > 1 || safePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(serverPage - 1).then(() => setPage((serverPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={safePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{safePage} / {totalPages}</span>
                <button onClick={() => {
                    if (chunkPage < uiPagesPerChunk && chunkPage * PAGE_SIZE < shown.length) { setPage((p) => Math.min(totalPages, p + 1)); return; }
                    if (!quoteHasMore && serverPage >= serverPages) { setPage((p) => Math.min(totalPages, p + 1)); return; }
                    gotoServerPage(serverPage + 1).then(() => setPage(serverPage * uiPagesPerChunk + 1));
                  }}
                  disabled={safePage >= totalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- News history table ---------- */}
      {tab === "news" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">Event</th>
              <th className="py-2 pr-3">Sentiment</th>
              <th className="py-2 pr-3">สินทรัพย์</th>
              <th className="py-2 pr-3">ความมั่นใจ</th>
              <th className="py-2 pr-3">ที่มา</th>
              {/* บทวิเคราะห์: ต้องมี min-width — ตาราง auto-layout + overflow-x-auto
                  บีบคอลัมน์นี้เหลือ ~96px บนมือถือ (ข้อความยาวต่อเนื่องหักบรรทัดได้
                  ทุกจุด) ทำให้บทวิเคราะห์ยาว 2-3 บรรทัดกลายเป็น 26 บรรทัด
                  แถวสูง 433px; ใส่ min-width แล้วตารางกว้างเกินจอ → wrapper เลื่อน
                  แนวนอนแทน (280px = อ่านสบาย วัดจริงเหลือ 8 บรรทัด / แถว 145px)
                  และยกเพดานเดิม 420px → 640px ให้เดสก์ท็อปอ่านสบายขึ้น */}
              <th className="py-2 min-w-[280px]">บทวิเคราะห์</th>
            </tr>
          </thead>
          <tbody>
            {loading && newsLogs.length === 0 && (
              <tr><td colSpan={7} className="py-6">
                <LoadingGraphic message="กำลังโหลดประวัติข่าว — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && newsLogs.length === 0 && (
              <tr><td colSpan={7} className="py-6 text-center text-slate-500">
                ยังไม่มีประวัติข่าว
              </td></tr>
            )}
            {newsPageRows.map((n) => {
              const isHeuristic = !n.analysis || n.analysis.includes("heuristic");
              const s = n.sentiment ?? 0;
              return (
              <tr key={n.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {n.created_at ? new Date(n.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                </td>
                <td className="py-2 pr-3 font-bold whitespace-nowrap">{n.event}</td>
                <td className="py-2 pr-3 whitespace-nowrap">
                  <span className={s > 0.2 ? "text-emerald-400" : s < -0.2 ? "text-red-400" : "text-slate-400"}>
                    {s > 0 ? `+${s.toFixed(2)}` : s.toFixed(2)}
                  </span>
                </td>
                <td className="py-2 pr-3 whitespace-nowrap text-slate-300">
                  {(n.affected_assets ?? []).join(", ") || "—"}
                </td>
                <td className="py-2 pr-3 text-slate-300">
                  {n.confidence != null ? `${Number(n.confidence).toFixed(0)}%` : "—"}
                </td>
                <td className="py-2 pr-3 whitespace-nowrap">
                  <span className={`px-2 py-0.5 rounded ${isHeuristic ? "bg-amber-500/15 text-amber-400" : "bg-emerald-500/15 text-emerald-400"}`}>
                    {isHeuristic ? "heuristic" : "พาดหัวจริง"}
                  </span>
                </td>
                <td className="py-2 min-w-[280px] max-w-[640px] text-slate-300" style={{ whiteSpace: "pre-wrap" }}>
                  {n.analysis || "—"}
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
        </div>
        {(newsLogs.length > 0 || newsTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {newsSafePage}/{newsTotalPages} · แสดง {newsPageRows.length} จาก {newsTotal.toLocaleString()} รายการ
              {newsServerPages > 1 && ` · ชุดที่ ${newsServerPage}/${newsServerPages}`}
            </p>
            {newsTotalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (newsChunkPage > 1 || newsSafePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(newsServerPage - 1).then(() => setPage((newsServerPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={newsSafePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{newsSafePage} / {newsTotalPages}</span>
                <button onClick={() => {
                    if (newsChunkPage < uiPagesPerChunk && newsChunkPage * PAGE_SIZE < newsChunkTotal) { setPage((p) => Math.min(newsTotalPages, p + 1)); return; }
                    if (!newsHasMore && newsServerPage >= newsServerPages) { setPage((p) => Math.min(newsTotalPages, p + 1)); return; }
                    gotoServerPage(newsServerPage + 1).then(() => setPage(newsServerPage * uiPagesPerChunk + 1));
                  }}
                  disabled={newsSafePage >= newsTotalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- Scheduler run table ---------- */}
      {tab === "scheduler" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">Job</th>
              <th className="py-2 pr-3">สถานะ</th>
              <th className="py-2 pr-3">ms</th>
              <th className="py-2 pr-3">ผลลัพธ์</th>
              <th className="py-2">Error</th>
            </tr>
          </thead>
          <tbody>
            {loading && schedLogs.length === 0 && (
              <tr><td colSpan={6} className="py-6">
                <LoadingGraphic message="กำลังโหลดประวัติ scheduler — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && schedLogs.length === 0 && (
              <tr><td colSpan={6} className="py-6 text-center text-slate-500">
                {schedulerEmptyMessage("scheduler", schedSummary)}
              </td></tr>
            )}
            {schedPageRows.map((s) => (
              <tr key={s.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {s.created_at ? new Date(s.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                </td>
                <td className="py-2 pr-3 font-bold whitespace-nowrap">{s.job_id}</td>
                <td className="py-2 pr-3">
                  <span className={`px-2 py-0.5 rounded ${s.status === "ok" ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"}`}>{s.status}</span>
                </td>
                <td className="py-2 pr-3 text-slate-400">{s.duration_ms ?? "—"}</td>
                <td className="py-2 pr-3 max-w-[320px] truncate text-slate-300" title={s.detail || ""}>{s.detail || "—"}</td>
                <td className="py-2 max-w-[280px] truncate" title={s.error || ""}><span className="text-loss">{s.error || "—"}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        {(schedLogs.length > 0 || schedTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {schedSafePage}/{schedTotalPages} · แสดง {schedPageRows.length} จาก {schedTotal.toLocaleString()} รายการ
              {schedServerPages > 1 && ` · ชุดที่ ${schedServerPage}/${schedServerPages}`} · เก็บสูงสุด {ttlDays} วัน
            </p>
            {schedTotalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (schedChunkPage > 1 || schedSafePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(schedServerPage - 1).then(() => setPage((schedServerPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={schedSafePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{schedSafePage} / {schedTotalPages}</span>
                <button onClick={() => {
                    if (schedChunkPage < uiPagesPerChunk && schedChunkPage * PAGE_SIZE < schedChunkTotal) { setPage((p) => Math.min(schedTotalPages, p + 1)); return; }
                    if (!schedHasMore && schedServerPage >= schedServerPages) { setPage((p) => Math.min(schedTotalPages, p + 1)); return; }
                    gotoServerPage(schedServerPage + 1).then(() => setPage(schedServerPage * uiPagesPerChunk + 1));
                  }}
                  disabled={schedSafePage >= schedTotalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- Gate run table (order_blocked / order_opened) ---------- */}
      {tab === "gate" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <div className="flex gap-2 mb-3 text-xs">
          {(["order_blocked", "order_opened", "all"] as const).map((f) => (
            <button
              key={f}
              onClick={async () => {
                setGateFilter(f); gateFilterRef.current = f; setPage(1);
                setLoading(true);
                try {
                  const res = await api.signalLogs(SERVER_PAGE, 0, f);
                  setGateLogs(res.logs ?? []);
                  setGateTotal(res.total ?? (res.logs ?? []).length);
                  setGateHasMore(res.has_more ?? false);
                  if (res.summary) setGateSummary(res.summary);
                } finally { setLoading(false); }
              }}
              className={`px-3 py-1 rounded ${gateFilter === f ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
              {f === "all" ? "ทั้งหมด" : f === "order_blocked" ? "ปัดตก" : "ผ่าน"}
            </button>
          ))}
        </div>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">Asset</th>
              <th className="py-2 pr-3">ทิศ</th>
              <th className="py-2 pr-3">ผล</th>
              <th className="py-2 pr-3">Conf</th>
              <th className="py-2 pr-3">Entry</th>
              <th className="py-2 pr-3">เหตุผล gate</th>
              <th className="py-2">Ticket</th>
            </tr>
          </thead>
          <tbody>
            {loading && gateLogs.length === 0 && (
              <tr><td colSpan={8} className="py-6">
                <LoadingGraphic message="กำลังโหลดประวัติ gate — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && gateLogs.length === 0 && (
              <tr><td colSpan={8} className="py-6 text-center text-slate-500">
                ยังไม่มีประวัติ gate ในช่วงนี้
              </td></tr>
            )}
            {gatePageRows.map((g) => {
              const blocked = g.event === "order_blocked";
              return (
              <tr key={g.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {g.created_at ? new Date(g.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                </td>
                <td className="py-2 pr-3 font-bold whitespace-nowrap">{g.asset || "—"}</td>
                <td className="py-2 pr-3 whitespace-nowrap">{(g.direction || "—").toUpperCase()}</td>
                <td className="py-2 pr-3">
                  <span className={`px-2 py-0.5 rounded whitespace-nowrap ${blocked ? "bg-amber-500/15 text-amber-400" : "bg-emerald-500/15 text-emerald-400"}`}>
                    {blocked ? "ปัดตก" : g.event === "order_opened" ? "ผ่าน" : g.event}
                  </span>
                </td>
                <td className="py-2 pr-3 text-slate-300">{g.confidence != null ? `${Number(g.confidence).toFixed(0)}` : "—"}</td>
                <td className="py-2 pr-3 font-mono text-slate-300">{g.entry != null ? fmtNum(g.entry, 4) : "—"}</td>
                <td className="py-2 pr-3 max-w-[420px] text-slate-300" style={{ whiteSpace: "pre-wrap" }} title={g.reason || ""}>{g.reason || "—"}</td>
                <td className="py-2 font-mono text-slate-500">{g.ticket || "—"}</td>
              </tr>
              );
            })}
          </tbody>
        </table>
        </div>
        {(gateLogs.length > 0 || gateTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {gateSafePage}/{gateTotalPages} · แสดง {gatePageRows.length} จาก {gateTotal.toLocaleString()} รายการ
              {gateServerPages > 1 && ` · ชุดที่ ${gateServerPage}/${gateServerPages}`} · เก็บสูงสุด {ttlDays} วัน
            </p>
            {gateTotalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (gateChunkPage > 1 || gateSafePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(gateServerPage - 1).then(() => setPage((gateServerPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={gateSafePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{gateSafePage} / {gateTotalPages}</span>
                <button onClick={() => {
                    if (gateChunkPage < uiPagesPerChunk && gateChunkPage * PAGE_SIZE < gateChunkTotal) { setPage((p) => Math.min(gateTotalPages, p + 1)); return; }
                    if (!gateHasMore && gateServerPage >= gateServerPages) { setPage((p) => Math.min(gateTotalPages, p + 1)); return; }
                    gotoServerPage(gateServerPage + 1).then(() => setPage(gateServerPage * uiPagesPerChunk + 1));
                  }}
                  disabled={gateSafePage >= gateTotalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- Guard run table (position_guard เท่านั้น) ---------- */}
      {tab === "guard" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">สถานะ</th>
              <th className="py-2 pr-3">ms</th>
              <th className="py-2 pr-3">รายละเอียดรอบนี้</th>
              <th className="py-2">Error</th>
            </tr>
          </thead>
          <tbody>
            {loading && guardLogs.length === 0 && (
              <tr><td colSpan={5} className="py-6">
                <LoadingGraphic message="กำลังโหลดประวัติ guard — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && guardLogs.length === 0 && (
              <tr><td colSpan={5} className="py-6 text-center text-slate-500">
                {schedulerEmptyMessage("guard", schedSummary)}
                {jobStats?.position_guard && (
                  <div className="pt-2 text-xs text-slate-400">
                    heartbeat: scheduler เรียก guard {jobStats.position_guard.ticks} ครั้ง · ok {jobStats.position_guard.ok} ·
                    error {jobStats.position_guard.error} · ข้าม {jobStats.position_guard.skipped}
                    {jobStats.position_guard.last_error && ` · error ล่าสุด: ${jobStats.position_guard.last_error}`}
                  </div>
                )}
              </td></tr>
            )}
            {guardPageRows.map((g) => {
              // รอบที่ถูกข้าม: "ตรวจ/ขยับ SL/ปิดไม้" เป็น 0 หมด จึงแสดง badge
              // "ข้าม" แทนสถานะ ok เพื่อไม่ให้เข้าใจว่า guard ทำงานแล้วได้ 0
              const skipped = (parseGuardDetail(g.detail ?? "").skipped_prev_running ?? 0) > 0;
              return (
              <tr key={g.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {g.created_at ? new Date(g.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                </td>
                <td className="py-2 pr-3">
                  {skipped
                    ? <span className="px-2 py-0.5 rounded bg-amber-500/15 text-amber-400" title="รอบก่อนยังไม่จบ — รอบนี้ถูกข้าม">ข้าม</span>
                    : <span className={`px-2 py-0.5 rounded ${g.status === "ok" ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"}`}>{g.status}</span>}
                </td>
                <td className="py-2 pr-3 text-slate-400">{g.duration_ms ?? "—"}</td>
                <td className="py-2 pr-3 min-w-[260px] max-w-[420px]">
                  <GuardDetailCell
                    detail={g.detail}
                    status={g.status}
                    createdAt={g.created_at}
                    durationMs={g.duration_ms}
                  />
                </td>
                <td className="py-2 max-w-[280px] truncate" title={g.error || ""}><span className="text-loss">{g.error || "—"}</span></td>
              </tr>
              );
            })}
          </tbody>
        </table>
        </div>
        {(guardLogs.length > 0 || guardTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {guardSafePage}/{guardTotalPages} · แสดง {guardPageRows.length} จาก {guardTotal.toLocaleString()} รอบ
              {guardServerPages > 1 && ` · ชุดที่ ${guardServerPage}/${guardServerPages}`} · เก็บสูงสุด {ttlDays} วัน
            </p>
            {guardTotalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (guardChunkPage > 1 || guardSafePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(guardServerPage - 1).then(() => setPage((guardServerPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={guardSafePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{guardSafePage} / {guardTotalPages}</span>
                <button onClick={() => {
                    if (guardChunkPage < uiPagesPerChunk && guardChunkPage * PAGE_SIZE < guardChunkTotal) { setPage((p) => Math.min(guardTotalPages, p + 1)); return; }
                    if (!guardHasMore && guardServerPage >= guardServerPages) { setPage((p) => Math.min(guardTotalPages, p + 1)); return; }
                    gotoServerPage(guardServerPage + 1).then(() => setPage(guardServerPage * uiPagesPerChunk + 1));
                  }}
                  disabled={guardSafePage >= guardTotalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- Audit summary (risk_events — ประวัติถาวร ไม่ถูกลบ) ---------- */}
      {tab === "audit" && (() => {
        const by = auditSummary?.by_event ?? {};
        const breached = by.limit_breach ?? 0;
        const expanded = by.limit_expanded ?? 0;
        const rejected = by.limit_expand_rejected ?? 0;
        const grand = Object.values(by).reduce((a, n) => a + n, 0);
        const latestReq = auditReqs[0];
        return (
        <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="panel">
            <p className="text-xs text-slate-500">เหตุการณ์ทั้งหมด</p>
            <p className="text-2xl font-bold">{grand.toLocaleString()}</p>
            <p className="text-xs text-slate-500 mt-1">เก็บถาวร — ไม่ถูกลบตามอายุเหมือน log อื่นในหน้านี้</p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">kill switch เข้าเงื่อนไข</p>
            <p className={`text-2xl font-bold ${breached > 0 ? "text-rose-400" : ""}`}>{breached.toLocaleString()}</p>
            <p className="text-xs text-slate-500 mt-1">limit_breach — เกินลิมิต → หยุดเทรด + เตือน LINE</p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">อนุมัติขยายลิมิต</p>
            <p className={`text-2xl font-bold ${expanded > 0 ? "text-amber-400" : ""}`}>{expanded.toLocaleString()}</p>
            <p className="text-xs text-slate-500 mt-1">limit_expanded — ขยายลิมิตแล้วไม่ปิดไม้</p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">ปฏิเสธคำขอขยาย</p>
            <p className={`text-2xl font-bold ${rejected > 0 ? "text-sky-400" : ""}`}>{rejected.toLocaleString()}</p>
            <p className="text-xs text-slate-500 mt-1">limit_expand_rejected — ไม่ขยายตามที่ขอ</p>
          </div>
          {latestReq && (
            <div className="panel col-span-2 md:col-span-4">
              <p className="text-xs text-slate-500">คำขอยืนยันขยายลิมิตล่าสุด (kill_expand_requests)</p>
              <p className="text-sm font-bold mt-1">
                {riskStatusLabel(latestReq.status)}
                {latestReq.limit_before != null && latestReq.limit_after != null && (
                  <span className="font-normal text-slate-300">
                    {" "}· ลิมิต {fmtNum(latestReq.limit_before, 1)}% → {fmtNum(latestReq.limit_after, 1)}%
                  </span>
                )}
              </p>
              <p className="text-xs mt-1 text-slate-400">
                {latestReq.trigger_type || "—"}
                {latestReq.metric_value != null && ` (ค่าที่วัดได้ ${fmtNum(latestReq.metric_value, 2)})`}
                {latestReq.requested_at && ` · ขอเมื่อ ${auditTime(latestReq.requested_at)}`}
                {` · ${decidedByLabel(latestReq.decided_by)}`}
              </p>
            </div>
          )}
        </section>
        );
      })()}

      {/* audit_hint = มีคำขอยืนยันแต่ risk_events ว่าง ⇒ เขียน audit ไม่ลง */}
      {tab === "audit" && auditHint && (
        <section className="panel border-amber-400/50">
          <p className="text-sm font-bold text-amber-300 flex items-center gap-1.5">
            <Icon n="warning" size={15} /> ตาราง risk_events ว่างเปล่า — เขียน audit ไม่ลง
          </p>
          <p className="text-xs mt-1 text-slate-300">{auditHint}</p>
        </section>
      )}

      {/* ---------- Audit table (risk_events = ร่องรอยการตัดสินใจ) ---------- */}
      {tab === "audit" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <div className="flex flex-wrap gap-2 mb-3 text-xs">
          {(["all", "limit_breach", "limit_expanded", "limit_expand_rejected"] as const).map((f) => (
            <button
              key={f}
              onClick={async () => {
                setAuditFilter(f); auditFilterRef.current = f; setPage(1);
                setLoading(true);
                try {
                  const res = await api.riskLogs(SERVER_PAGE, 0, f);
                  setAuditLogs(res.logs ?? []);
                  setAuditReqs(res.requests ?? []);
                  setAuditHint(res.audit_hint ?? "");
                  setAuditTotal(res.summary?.total ?? (res.logs ?? []).length);
                  setAuditHasMore(res.has_more ?? false);
                } finally { setLoading(false); }
              }}
              className={`px-3 py-1 rounded ${auditFilter === f ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
              {f === "all" ? "ทั้งหมด" : riskEventShort(f)}
            </button>
          ))}
        </div>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">เหตุการณ์</th>
              <th className="py-2 pr-3">รายละเอียดการตัดสินใจ</th>
              <th className="py-2">Request</th>
            </tr>
          </thead>
          <tbody>
            {loading && auditLogs.length === 0 && (
              <tr><td colSpan={4} className="py-6">
                <LoadingGraphic message="กำลังโหลดประวัติความเสี่ยง — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && auditLogs.length === 0 && (
              <tr><td colSpan={4} className="py-6 text-center text-slate-500">
                {auditReqs.length > 0
                  ? "มีคำขอยืนยันขยายลิมิตแต่ยังไม่มีเหตุการณ์ที่บันทึกได้ — ดูคำเตือนด้านบน (migration 038)"
                  : "ยังไม่มีเหตุการณ์ความเสี่ยง — ระบบเขียนที่นี่ทุกครั้งที่ kill switch เข้าเงื่อนไข หรือเจ้าของอนุมัติ/ปฏิเสธขยายลิมิต"}
              </td></tr>
            )}
            {auditPageRows.map((ev) => (
              <tr key={ev.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">{auditTime(ev.created_at)}</td>
                <td className="py-2 pr-3">
                  <span className={`px-2 py-0.5 rounded whitespace-nowrap ${riskEventTone(ev.event_type)}`}>
                    {riskEventLabel(ev.event_type)}
                  </span>
                </td>
                <td className="py-2 pr-3 min-w-[280px] max-w-[560px]">
                  <AuditDetailCell ev={ev} />
                </td>
                <td className="py-2 font-mono text-[11px] text-slate-500">
                  {ev.detail?.request_id ? String(ev.detail.request_id).slice(0, 8) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        {(auditLogs.length > 0 || auditTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {auditSafePage}/{auditTotalPages} · แสดง {auditPageRows.length} จาก {auditTotal.toLocaleString()} รายการ
              {auditServerPages > 1 && ` · ชุดที่ ${auditServerPage}/${auditServerPages}`} · เก็บถาวร (ไม่มี TTL)
            </p>
            {auditTotalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (auditChunkPage > 1 || auditSafePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(auditServerPage - 1).then(() => setPage((auditServerPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={auditSafePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{auditSafePage} / {auditTotalPages}</span>
                <button onClick={() => {
                    if (auditChunkPage < uiPagesPerChunk && auditChunkPage * PAGE_SIZE < auditChunkTotal) { setPage((p) => Math.min(auditTotalPages, p + 1)); return; }
                    if (!auditHasMore && auditServerPage >= auditServerPages) { setPage((p) => Math.min(auditTotalPages, p + 1)); return; }
                    gotoServerPage(auditServerPage + 1).then(() => setPage(auditServerPage * uiPagesPerChunk + 1));
                  }}
                  disabled={auditSafePage >= auditTotalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- คำขอยืนยันขยายลิมิต (kill_expand_requests) ---------- */}
      {tab === "audit" && auditReqs.length > 0 && (
      <section className="panel">
        <p className="text-sm font-bold">คำขอยืนยันขยายลิมิต (ล่าสุด {auditReqs.length} รายการ)</p>
        <p className="text-xs text-slate-500 mt-0.5 mb-3">
          ต้นทางของการตัดสินใจ — ระบบขอให้เจ้าของยืนยันก่อนขยายลิมิต เพราะการขยาย =
          ยอมรับความเสี่ยงที่มากขึ้น · คำขอที่อนุมัติแล้วคือลิมิตที่ระบบใช้อยู่จริง
        </p>
        <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">ขอเมื่อ</th>
              <th className="py-2 pr-3">สถานะ</th>
              <th className="py-2 pr-3">เข้าเงื่อนไข</th>
              <th className="py-2 pr-3">ลิมิตก่อน → หลัง</th>
              <th className="py-2 pr-3">ตัดสินเมื่อ</th>
              <th className="py-2">ใครตัดสิน</th>
            </tr>
          </thead>
          <tbody>
            {auditReqs.map((r) => (
              <tr key={r.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">{auditTime(r.requested_at)}</td>
                <td className="py-2 pr-3">
                  <span className={`px-2 py-0.5 rounded whitespace-nowrap ${riskStatusTone(r.status)}`}>
                    {riskStatusLabel(r.status)}
                  </span>
                </td>
                <td className="py-2 pr-3 text-slate-300">
                  {r.trigger_type || "—"}
                  {r.metric_value != null && <span className="text-slate-500"> (วัดได้ {fmtNum(r.metric_value, 2)})</span>}
                </td>
                <td className="py-2 pr-3 font-mono text-slate-300">
                  {r.limit_before != null && r.limit_after != null
                    ? `${fmtNum(r.limit_before, 1)}% → ${fmtNum(r.limit_after, 1)}%`
                    : "—"}
                </td>
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">{auditTime(r.decided_at)}</td>
                <td className="py-2 text-slate-300">{decidedByLabel(r.decided_by)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </section>
      )}
    </div>
  );
}
