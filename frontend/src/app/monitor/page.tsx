"use client";

import { useCallback, useEffect, useState } from "react";
import ClosePositionModal from "@/components/ClosePositionModal";
import FeedStatusBanner from "@/components/FeedStatusBanner";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { ClosePositionResult, MonitorSnapshot } from "@/lib/types";

// ความถี่รีเฟรชเลือกได้จาก UI (จำค่าใน localStorage) — backend cache spot
// quotes 30s ดังนั้นยิงถี่กว่า 10s ก็ไม่เพิ่มโหลด feed
const REFRESH_OPTIONS = [
  { label: "ปิด", value: 0 },
  { label: "10 วิ", value: 10 },
  { label: "30 วิ", value: 30 },
  { label: "1 นาที", value: 60 },
  { label: "5 นาที", value: 300 },
];

const LS_KEY = "tdapp_monitor_autorefresh";

export default function MonitorPage() {
  const [snap, setSnap] = useState<MonitorSnapshot | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string>("");
  // อ่านค่าตั้งต้นจาก localStorage (จำค่าที่เลือกไว้) — init function กัน SSR mismatch
  const [intervalSec, setIntervalSec] = useState<number>(() => {
    if (typeof window === "undefined") return 10;
    const saved = Number(window.localStorage.getItem(LS_KEY));
    return REFRESH_OPTIONS.some((o) => o.value === saved) ? saved : 10;
  });
  const [closeResult, setCloseResult] = useState<ClosePositionResult | null>(null);
  const [closeError, setCloseError] = useState("");
  const [closingTicket, setClosingTicket] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [resetMsg, setResetMsg] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const s = await api.monitor();
      setSnap(s);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    if (intervalSec <= 0) return; // ปิดรีเฟรชอัตโนมัติ
    const t = setInterval(load, intervalSec * 1000);
    return () => clearInterval(t);
  }, [load, intervalSec]);

  const changeInterval = (v: number) => {
    setIntervalSec(v);
    if (typeof window !== "undefined") window.localStorage.setItem(LS_KEY, String(v));
  };

  const togglePause = async () => {
    setBusy(true);
    try {
      await api.setTradingPause(!(snap?.pause.paused ?? false), "");
      await load();
    } finally {
      setBusy(false);
    }
  };

  // ปิดไม้ด้วยมือ → เด้ง popup สรุปกำไร/ขาดทุน
  const handleClosePosition = async (ticket: string) => {
    if (!ticket) return;
    setClosingTicket(ticket);
    setCloseError("");
    try {
      const res = await api.closePosition(ticket);
      if (res.ok) {
        setCloseResult(res);
        await load();
      } else {
        setCloseError(res.message);
      }
    } catch (e) {
      setCloseError(e instanceof Error ? e.message : String(e));
    } finally {
      setClosingTicket(null);
    }
  };

  // รีเซ็ตสถิติ — ลบไม้ที่ปิดแล้วทั้งหมด (ไม้เปิดค้างไม่ถูกแตะ) → PnL/Win Rate กลับเป็น 0
  const handleResetStats = async () => {
    if (resetting) return;
    const ok = window.confirm(
      "รีเซ็ตสถิติการเทรด?\n\n" +
      "• ลบไม้ที่ปิดแล้วทั้งหมด (PnL วันนี้ / 7 วัน / รวม, Win Rate กลับเป็น 0)\n" +
      "• ไม้ที่เปิดค้างจะไม่ถูกลบ — SL/TP ยังทำงานตามปกติ\n" +
      "• ทำแล้วย้อนกลับไม่ได้");
    if (!ok) return;
    setResetting(true);
    setResetMsg("");
    try {
      const res = await api.resetStats();
      setResetMsg(res.ok ? res.message : `รีเซ็ตไม่สำเร็จ: ${res.message}`);
      await load();
    } catch (e) {
      setResetMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setResetting(false);
    }
  };

  const st = snap?.stats;

  return (
    <div className="space-y-4">
      <FeedStatusBanner feed={snap?.feed_status} />
      {/* ---------- Status strip ---------- */}
      <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div className={`panel ${snap?.pause.paused ? "border-loss" : ""}`}>
          <p className="text-xs text-slate-500">Execution Switch</p>
          {snap?.pause.paused ? (
            <>
              <p className="text-lg font-bold text-loss">🛑 PAUSED</p>
              <p className="text-xs text-slate-400 truncate">{snap.pause.reason || "manual"}</p>
            </>
          ) : (
            <p className="text-lg font-bold text-profit">▶️ Active</p>
          )}
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">โหมดเทรด</p>
          <p className="text-lg font-bold">
            {snap?.order_mode === "auto" ? "🤖 Auto"
              : snap?.order_mode === "semi_auto" ? "👤 Semi-Auto" : "✋ Manual"}
          </p>
        </div>
        <div className={`panel ${snap?.kill.engaged ? "border-loss" : ""}`}>
          <p className="text-xs text-slate-500">Kill Switch</p>
          {snap?.kill.engaged ? (
            <>
              <p className="text-lg font-bold text-loss">ENGAGED</p>
              <p className="text-xs text-slate-400 truncate">{snap.kill.message}</p>
            </>
          ) : (
            <p className="text-lg font-bold text-profit">Clear</p>
          )}
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">ไม้เปิดค้าง</p>
          <p className="text-lg font-bold">{st?.open_positions ?? "-"}</p>
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">Capital</p>
          <p className="text-lg font-bold">${fmtNum(snap?.capital ?? 0, 0)}</p>
        </div>
      </section>

      {/* ---------- Stats ---------- */}
      <section className="space-y-2">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h2 className="panel-title">สถิติการเทรด</h2>
          <button onClick={handleResetStats} disabled={resetting}
            className="border border-slate-700 rounded px-3 py-2 text-sm min-h-[40px] text-slate-300 active:bg-slate-800 disabled:opacity-50"
            title="ลบไม้ที่ปิดแล้วทั้งหมด — ไม้ที่เปิดค้างไม่ถูกลบ">
            {resetting ? "กำลังรีเซ็ต..." : "🗑 รีเซ็ตสถิติ"}
          </button>
        </div>
        {resetMsg && (
          <p className="text-xs text-slate-400 bg-slate-800/60 border border-slate-700 rounded px-3 py-2">
            {resetMsg}
          </p>
        )}
        <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <div className="panel">
            <p className="text-xs text-slate-500">เทรดวันนี้</p>
            <p className="text-xl font-bold">{st?.trades_today ?? "-"}</p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">PnL วันนี้</p>
            <PnlText v={st?.pnl_today} />
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">PnL 7 วัน</p>
            <PnlText v={st?.pnl_week} />
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">PnL รวม (ปิดแล้ว)</p>
            <PnlText v={st?.pnl_total} />
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">Win Rate</p>
            <p className={`text-xl font-bold ${(st?.win_rate ?? 0) >= 50 ? "text-profit" : "text-loss"}`}>
              {st ? `${fmtNum(st.win_rate, 1)}%` : "-"}
            </p>
            <p className="text-xs text-slate-500">{st?.closed_count ?? 0} ไม้ที่ปิดแล้ว</p>
          </div>
        </section>
      </section>

      {/* ---------- Open positions ---------- */}
      <div className="panel">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h2 className="panel-title">ไม้ที่เปิดค้าง (Paper)</h2>
          <div className="flex items-center gap-3">
            {updatedAt && <span className="text-xs text-slate-500">อัปเดต {updatedAt}</span>}
            <select
              value={intervalSec}
              onChange={(e) => changeInterval(Number(e.target.value))}
              className="bg-surface border border-slate-700 rounded px-2 py-2 text-xs min-h-[40px]"
              aria-label="ตั้งเวลารีเฟรชอัตโนมัติ"
            >
              {REFRESH_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  รีเฟรช: {o.label}
                </option>
              ))}
            </select>
            <button onClick={togglePause} disabled={busy || !snap}
              className={snap?.pause.paused
                ? "bg-profit text-surface font-semibold rounded px-3 py-2 text-sm min-h-[40px] disabled:opacity-50 active:brightness-90"
                : "bg-loss text-surface font-semibold rounded px-3 py-2 text-sm min-h-[40px] disabled:opacity-50 active:brightness-90"}>
              {busy ? "..." : snap?.pause.paused ? "▶️ Resume" : "⏸️ Pause"}
            </button>
            <button onClick={load} disabled={loading}
              className="border border-slate-700 rounded px-3 py-2 text-sm min-h-[40px] text-slate-300 active:bg-slate-800 disabled:opacity-50">
              {loading ? "กำลังโหลด..." : "🔄 รีเฟรช"}
            </button>
          </div>
        </div>
        {err && <p className="text-loss text-sm mt-2">โหลดไม่สำเร็จ: {err}</p>}
        {closeError && (
          <p className="text-loss text-sm mt-2 bg-loss/10 border border-loss/40 rounded px-3 py-2">
            ปิดไม้ไม่สำเร็จ: {closeError}
          </p>
        )}

        {snap && snap.open_positions.length === 0 && (
          <p className="text-slate-500 text-sm mt-3">ไม่มีไม้เปิดค้าง — auto trader จะยิงเมื่อเจอ signal ที่ผ่าน gate</p>
        )}
        {snap && snap.open_positions.length > 0 && (
          <div className="overflow-x-auto mt-3">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500 uppercase tracking-wide">
                  <th className="py-2 pr-4">Asset</th>
                  <th className="py-2 pr-4">ฝั่ง</th>
                  <th className="py-2 pr-4">Lots</th>
                  <th className="py-2 pr-4">Entry</th>
                  <th className="py-2 pr-4">ราคาปัจจุบัน</th>
                  <th className="py-2 pr-4">SL</th>
                  <th className="py-2 pr-4">TP</th>
                  <th className="py-2 pr-4">PnL (ยังไม่ปิด)</th>
                  <th className="py-2 pr-4">ที่มา</th>
                  <th className="py-2 pr-4">Ticket</th>
                  <th className="py-2">จัดการ</th>
                </tr>
              </thead>
              <tbody>
                {snap.open_positions.map((p) => (
                  <tr key={p.id} className="border-t border-slate-800">
                    <td className="py-2 pr-4 font-semibold">{p.asset}</td>
                    <td className={`py-2 pr-4 font-bold ${p.direction === "BUY" ? "text-profit" : "text-loss"}`}>
                      {p.direction === "BUY" ? "▲ BUY" : "▼ SELL"}
                    </td>
                    <td className="py-2 pr-4">{fmtNum(p.volume, 2)}</td>
                    <td className="py-2 pr-4">{fmtNum(p.entry_price, 5)}</td>
                    <td className="py-2 pr-4">{fmtNum(p.current_price, 5)}</td>
                    <td className="py-2 pr-4 text-loss">{p.stop_loss != null ? fmtNum(p.stop_loss, 5) : "-"}</td>
                    <td className="py-2 pr-4 text-profit">{p.take_profit != null ? fmtNum(p.take_profit, 5) : "-"}</td>
                    <td className={`py-2 pr-4 font-bold ${p.unrealized_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                      {p.unrealized_pnl >= 0 ? "+" : ""}${fmtNum(p.unrealized_pnl, 2)}
                    </td>
                    <td className="py-2 pr-4 text-xs">{p.source === "auto" ? "🤖 Auto" : "👤 Approve"}</td>
                    <td className="py-2 text-xs text-slate-500">{p.ticket || "-"}</td>
                    <td className="py-2">
                      <button
                        onClick={() => handleClosePosition(p.ticket)}
                        disabled={!p.ticket || closingTicket === p.ticket}
                        className="bg-loss text-surface font-semibold rounded px-3 py-2 text-xs min-h-[40px] disabled:opacity-50 active:brightness-90"
                      >
                        {closingTicket === p.ticket ? "..." : "✋ ปิด"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ---------- Recent executions ---------- */}
      <div className="panel">
        <h2 className="panel-title">ประวัติการยิง order ล่าสุด</h2>
        {snap && snap.recent.length === 0 && (
          <p className="text-slate-500 text-sm mt-3">ยังไม่มีประวัติ — รอ signal แรกผ่าน gate</p>
        )}
        {snap && snap.recent.length > 0 && (
          <div className="overflow-x-auto mt-3">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500 uppercase tracking-wide">
                  <th className="py-2 pr-4">เวลา</th>
                  <th className="py-2 pr-4">Asset</th>
                  <th className="py-2 pr-4">ฝั่ง</th>
                  <th className="py-2 pr-4">Lots</th>
                  <th className="py-2 pr-4">Entry</th>
                  <th className="py-2 pr-4">Exit</th>
                  <th className="py-2 pr-4">PnL</th>
                  <th className="py-2 pr-4">สถานะ</th>
                  <th className="py-2 pr-4">เหตุผลปิด</th>
                  <th className="py-2">ที่มา</th>
                </tr>
              </thead>
              <tbody>
                {snap.recent.map((t) => (
                  <tr key={t.id} className="border-t border-slate-800">
                    <td className="py-2 pr-4 text-xs text-slate-400">
                      {t.created_at ? new Date(t.created_at).toLocaleString("th-TH") : "-"}
                    </td>
                    <td className="py-2 pr-4 font-semibold">{t.asset}</td>
                    <td className={`py-2 pr-4 font-bold ${t.direction === "BUY" ? "text-profit" : "text-loss"}`}>
                      {t.direction === "BUY" ? "▲" : "▼"} {t.direction}
                    </td>
                    <td className="py-2 pr-4">{fmtNum(t.volume, 2)}</td>
                    <td className="py-2 pr-4">{fmtNum(t.entry_price, 5)}</td>
                    <td className="py-2 pr-4">{t.exit_price != null ? fmtNum(t.exit_price, 5) : "-"}</td>
                    <td className="py-2 pr-4">
                      {t.pnl != null ? (
                        <span className={`font-bold ${t.pnl >= 0 ? "text-profit" : "text-loss"}`}>
                          {t.pnl >= 0 ? "+" : ""}${fmtNum(t.pnl, 2)}
                        </span>
                      ) : "-"}
                    </td>
                    <td className="py-2 pr-4">
                      <StatusBadge status={t.status} />
                    </td>
                    <td className="py-2 pr-4 text-xs">
                      {t.close_reason === "sl" ? "🛑 ตัดขาดทุน (SL)"
                        : t.close_reason === "tp" ? "🎯 ถึงเป้า (TP)"
                        : t.close_reason === "manual" ? "✋ ปิดเอง"
                        : "-"}
                    </td>
                    <td className="py-2 text-xs">{t.source === "auto" ? "🤖 Auto" : "👤 Approve"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ---------- Popup สรุปผลการปิดไม้ ---------- */}
      <ClosePositionModal result={closeResult} onClose={() => setCloseResult(null)} />
    </div>
  );
}

function PnlText({ v }: { v?: number }) {
  if (v == null) return <p className="text-xl font-bold">-</p>;
  return (
    <p className={`text-xl font-bold ${v >= 0 ? "text-profit" : "text-loss"}`}>
      {v >= 0 ? "+" : ""}${fmtNum(v, 2)}
    </p>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    open: { label: "เปิดอยู่", cls: "text-accent" },
    closed: { label: "ปิดแล้ว", cls: "text-slate-400" },
    rejected: { label: "ถูกบล็อก", cls: "text-loss" },
  };
  const it = map[status] ?? { label: status, cls: "text-slate-400" };
  return <span className={`text-xs font-semibold ${it.cls}`}>{it.label}</span>;
}
