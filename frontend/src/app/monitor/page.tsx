"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { MonitorSnapshot } from "@/lib/types";

const REFRESH_MS = 15_000;

export default function MonitorPage() {
  const [snap, setSnap] = useState<MonitorSnapshot | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string>("");

  const load = useCallback(async () => {
    try {
      const s = await api.monitor();
      setSnap(s);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  const togglePause = async () => {
    setBusy(true);
    try {
      await api.setTradingPause(!(snap?.pause.paused ?? false), "");
      await load();
    } finally {
      setBusy(false);
    }
  };

  const st = snap?.stats;

  return (
    <div className="space-y-4">
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

      {/* ---------- Open positions ---------- */}
      <div className="panel">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h2 className="panel-title">ไม้ที่เปิดค้าง (Paper)</h2>
          <div className="flex items-center gap-3">
            {updatedAt && <span className="text-xs text-slate-500">อัปเดต {updatedAt}</span>}
            <button onClick={togglePause} disabled={busy || !snap}
              className={snap?.pause.paused
                ? "bg-profit text-surface font-semibold rounded px-3 py-1.5 text-sm disabled:opacity-50"
                : "bg-loss text-surface font-semibold rounded px-3 py-1.5 text-sm disabled:opacity-50"}>
              {busy ? "..." : snap?.pause.paused ? "▶️ Resume" : "⏸️ Pause"}
            </button>
            <button onClick={load} disabled={busy}
              className="border border-slate-700 rounded px-3 py-1.5 text-sm text-slate-300 hover:text-accent disabled:opacity-50">
              🔄 รีเฟรช
            </button>
          </div>
        </div>
        {err && <p className="text-loss text-sm mt-2">โหลดไม่สำเร็จ: {err}</p>}

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
                  <th className="py-2">Ticket</th>
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
