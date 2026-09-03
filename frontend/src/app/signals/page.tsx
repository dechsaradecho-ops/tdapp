"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import SignalCard from "@/components/SignalCard";
import { api } from "@/lib/api";
import { SignalProposal } from "@/lib/types";

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
      const data = await api.latestSignals();
      setSignals(data);
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

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="panel-title">SEMI-AUTO — ข้อเสนอการเทรด (รอการอนุมัติ)</h2>

        <div className="flex items-center gap-2 text-sm">
          {lastUpdated && (
            <span className="text-xs text-slate-500">
              อัปเดตล่าสุด {lastUpdated.toLocaleTimeString("th-TH")}
            </span>
          )}

          <select
            value={intervalSec}
            onChange={(e) => changeInterval(Number(e.target.value))}
            className="bg-surface border border-slate-700 rounded px-2 py-1.5 text-xs"
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
            className="border border-accent text-accent rounded px-3 py-1.5 text-xs font-semibold hover:bg-accent/10 disabled:opacity-50"
          >
            {loading ? "กำลังโหลด..." : "↻ รีเฟรช"}
          </button>
        </div>
      </div>

      {error && <p className="text-loss text-sm">{error}</p>}
      {!signals.length && !error && !loading && (
        <p className="text-slate-500 text-sm">ยังไม่มีสัญญาณ — รอ Market Scanner</p>
      )}
      {/* รออนุมัติ — action queue ด้านบน */}
      {signals.some((s) => s.approval !== "approved") && (
        <>
          <h3 className="text-sm font-semibold text-slate-300">
            รอการอนุมัติ ({signals.filter((s) => s.approval !== "approved").length})
          </h3>
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
            {signals.filter((s) => s.approval !== "approved").map((s) => (
              <SignalCard key={`${s.asset}-${s.approval ?? "pending"}`} signal={s} />
            ))}
          </div>
        </>
      )}
      {/* อนุมัติแล้ว — แสดงด้านล่างพร้อมสแตมป์เวลาที่อนุมัติ */}
      {signals.some((s) => s.approval === "approved") && (
        <>
          <h3 className="text-sm font-semibold text-profit">
            อนุมัติแล้ว ({signals.filter((s) => s.approval === "approved").length})
          </h3>
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
            {signals.filter((s) => s.approval === "approved").map((s) => (
              <SignalCard key={`${s.asset}-${s.approved_at ?? "approved"}`} signal={s} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
