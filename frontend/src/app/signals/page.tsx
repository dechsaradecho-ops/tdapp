"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import FeedStatusBanner from "@/components/FeedStatusBanner";
import SignalCard from "@/components/SignalCard";
import { api } from "@/lib/api";
import { AppSettings, SessionStatus, SignalProposal } from "@/lib/types";

const REFRESH_OPTIONS = [
  { label: "ปิด", value: 0 },
  { label: "10 วิ", value: 10 },
  { label: "30 วิ", value: 30 },
  { label: "1 นาที", value: 60 },
  { label: "5 นาที", value: 300 },
];

const LS_KEY = "tdapp_signals_autorefresh";

export default function SignalsPage() {
  const [signals, setSignals] = useState<SignalProposal[]>([]);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [session, setSession] = useState<SessionStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  // อ่านค่าตั้งต้นจาก localStorage (จำค่าที่เลือกไว้) — init function กัน SSR mismatch
  const [intervalSec, setIntervalSec] = useState<number>(() => {
    if (typeof window === "undefined") return 0;
    const saved = Number(window.localStorage.getItem(LS_KEY));
    return REFRESH_OPTIONS.some((o) => o.value === saved) ? saved : 0;
  });
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) return; // กันยิงซ้อนระหว่าง request เก่ายังค้าง
    inFlight.current = true;
    setLoading(true);
    try {
      // settings อาจล้มเหลวได้ (auth/expiry) — หน้ายังแสดง signals ได้ปกติ
      const [data, st, sess] = await Promise.all([
        api.latestSignals(),
        api.getSettings().catch(() => null),
        api.tradingSession().catch(() => null),
      ]);
      setSignals(data);
      if (st) setSettings(st);
      if (sess) setSession(sess);
      setError(null);
      setLastUpdated(new Date());
    } catch (e) {
      setError(String(e));
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, []);

  // โหลดครั้งแรกเมื่อเข้าหน้า
  useEffect(() => {
    refresh();
  }, [refresh]);

  // รีเฟรชอัตโนมัติตามช่วงที่เลือก (0 = ปิด)
  useEffect(() => {
    if (intervalSec <= 0) return;
    const id = setInterval(refresh, intervalSec * 1000);
    return () => clearInterval(id);
  }, [intervalSec, refresh]);

  const changeInterval = (v: number) => {
    setIntervalSec(v);
    try {
      window.localStorage.setItem(LS_KEY, String(v));
    } catch {
      /* localStorage ใช้ไม่ได้ — ข้าม */
    }
  };

  // หัวข้อ + ป้ายตามโหมดจริงจาก settings — หน้าเคยเขียนตายตัวว่า SEMI-AUTO
  // ทั้งที่ order_mode=auto (auto trader ยิงเองใน ~1 นาที)
  const mode = settings?.order_mode ?? "semi_auto";
  const isAuto = mode === "auto";
  const heading = isAuto
    ? "🤖 AUTO — ระบบยิงออเดอร์เองเมื่อสัญญาณผ่านทุก gate"
    : mode === "manual"
      ? "✋ MANUAL — ทุกไม้ต้องกดอนุมัติเองก่อนยิง"
      : "SEMI-AUTO — ข้อเสนอการเทรด (รอการอนุมัติ)";
  const pending = signals.filter((s) => s.approval !== "approved");
  const approved = signals.filter((s) => s.approval === "approved");
  // เรียงใหม่ → เก่า (ผู้ใช้ขอ 2026-09-04): approved ใหม่ล่าสุดก่อน แล้วตามด้วย
  // pending ใหม่ล่าสุดก่อน — การ์ดใหม่ล่าสุดอยู่บนสุดของแต่ละหมวด
  const byTimeDesc = (a: SignalProposal, b: SignalProposal) =>
    String(b.approved_at ?? b.created_at ?? "")
      .localeCompare(String(a.approved_at ?? a.created_at ?? ""));
  approved.sort(byTimeDesc);
  pending.sort(byTimeDesc);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="panel-title">{heading}</h2>

        <div className="flex items-center gap-2 text-sm">
          {lastUpdated && (
            <span className="text-xs text-slate-500">
              อัปเดตล่าสุด {lastUpdated.toLocaleTimeString("th-TH")}
            </span>
          )}

          <select
            value={intervalSec}
            onChange={(e) => changeInterval(Number(e.target.value))}
            className="bg-surface border border-slate-700 rounded px-2 py-2 text-xs min-h-[40px]"
            aria-label="ตั้งเวลารีเฟรชอัตโนมัติ"
          >
            {REFRESH_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                รีเฟรชอัตโนมัติ: {o.label}
              </option>
            ))}
          </select>

          <button
            onClick={refresh}
            disabled={loading}
            className="border border-accent text-accent rounded px-3 py-2 text-xs min-h-[40px] font-semibold active:bg-accent/10 disabled:opacity-50"
          >
            {loading ? "กำลังโหลด..." : "↻ รีเฟรช"}
          </button>
        </div>
      </div>

      {error && <p className="text-loss text-sm">{error}</p>}
      {/* ตลาดปิด (สุดสัปดาห์) — สแกนเนอร์หยุดสร้างสัญญาณ ราคาบนการ์ดเก่าคือราคาปิดวันศุกร์ */}
      {session?.market_closed && (
        <div className="border border-amber-500/40 bg-amber-500/10 rounded p-3 text-sm text-amber-300">
          🔒 <b>ตลาดปิดอยู่</b> — ตลาด FX/ทองคำปิดสุดสัปดาห์ (ศุกร์ 21:00 UTC → อาทิตย์ 21:00 UTC)
          ระบบงดสร้างสัญญาณใหม่จนกว่าตลาดจะเปิด
          {session.next_open_utc &&
            ` — เปิดอีกครั้ง ${new Date(session.next_open_utc).toLocaleString("th-TH", { timeZone: "Asia/Bangkok" })} (เวลาไทย)`}
        </div>
      )}
      {/* สถานะฟีดราคา — ทุกการ์ดแชร์ probe เดียวกันต่อ request */}
      <FeedStatusBanner feed={signals[0]?.feed_status} />
      {!signals.length && !error && !loading && (
        <p className="text-slate-500 text-sm">
          {session?.market_closed
            ? "ตลาดปิด — ไม่มีสัญญาณใหม่ในช่วงนี้"
            : "ยังไม่มีสัญญาณ — รอ Market Scanner"}
        </p>
      )}
      {/* รอดำเนินการ — action queue ด้านบน (auto = ระบบยิงเอง, semi/manual = รอกด)
          กลุ่มพับ/กางได้ (ค่าเริ่มต้น: กาง) — defaultExpanded กัน hydration mismatch */}
      {pending.length > 0 && (
        <SignalGroup
          title={isAuto
            ? `🤖 ระบบกำลังดำเนินการ (${pending.length}) — การ์ดไหนพร้อมยิง ระบบจะเปิดออเดอร์ให้ภายใน ~1 นาที`
            : `รอการอนุมัติ (${pending.length})`}
          count={pending.length}
          defaultExpanded
        >
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
            {pending.map((s) => (
              <SignalCard key={`${s.asset}-${s.approval ?? "pending"}`} signal={s} orderMode={mode} />
            ))}
          </div>
        </SignalGroup>
      )}
      {/* อนุมัติ/ยิงแล้ว — แสดงด้านล่างพร้อมสแตมป์เวลา */}
      {approved.length > 0 && (
        <SignalGroup
          title={isAuto ? `🤖 ยิงออเดอร์แล้ว (${approved.length})` : `อนุมัติแล้ว (${approved.length})`}
          count={approved.length}
          defaultExpanded={false}
        >
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
            {approved.map((s) => (
              <SignalCard key={`${s.asset}-${s.approved_at ?? "approved"}`} signal={s} orderMode={mode} />
            ))}
          </div>
        </SignalGroup>
      )}
    </div>
  );
}

/** หัวข้อกลุ่มที่กดพับ/กางได้ — chevron หมุนตามสถานะ */
function SignalGroup({ title, count, defaultExpanded, children }: {
  title: string;
  count: number;
  defaultExpanded: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultExpanded);
  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between rounded-lg bg-surface border border-slate-800 px-3 py-2.5 min-h-[44px] active:brightness-90 transition"
        aria-expanded={open}
      >
        <span className={`text-sm font-semibold ${title.includes("ยิง") || title.includes("อนุมัติ") ? "text-profit" : "text-slate-300"}`}>
          {title}
        </span>
        <span className="flex items-center gap-2 text-xs text-slate-500">
          <span>{count} รายการ</span>
          <svg
            className={`w-4 h-4 transition-transform duration-200 ${open ? "rotate-180" : ""}`}
            fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" aria-hidden
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </span>
      </button>
      {open && <div className="mt-3">{children}</div>}
    </div>
  );
}
