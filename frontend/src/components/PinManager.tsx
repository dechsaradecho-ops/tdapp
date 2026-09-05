"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { clearToken, setToken } from "@/lib/auth";
import { PinStatus } from "@/lib/types";

/** Settings panel: first-time PIN setup + change PIN (requires session). */
export default function PinManager() {
  const [status, setStatus] = useState<PinStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusErr, setStatusErr] = useState(false);
  const [pin, setPin] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState("");
  const [ok, setOk] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => {
    setStatusLoading(true);
    return api.authStatus()
      .then((s) => { setStatus(s); setStatusErr(false); })
      .catch(() => { setStatus(null); setStatusErr(true); })
      .finally(() => setStatusLoading(false));
  };
  useEffect(() => { load(); }, []);

  const submit = async () => {
    setMsg(""); setOk(null);
    if (pin.length !== 6 || !/^\d{6}$/.test(pin)) {
      setOk(false); setMsg("PIN ต้องเป็นตัวเลข 6 หลัก"); return;
    }
    if (pin !== confirm) {
      setOk(false); setMsg("PIN ทั้งสองช่องไม่ตรงกัน"); return;
    }
    setBusy(true);
    try {
      const r = await api.authSetPin(pin);
      setOk(r.ok);
      setMsg(r.message || (r.ok ? "บันทึกแล้ว" : "ตั้ง PIN ไม่สำเร็จ"));
      if (r.ok) {
        // set-pin returns a fresh token — SAVE it, otherwise the very next
        // API call 401s and the gate locks the owner out right after setup.
        if (r.token) setToken(r.token);
        setPin(""); setConfirm("");
        await load();
      }
    } catch (e) {
      setOk(false);
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const logout = async () => {
    try { await api.authLogout(); } catch { /* already dead */ }
    clearToken();
    window.location.href = "/";
  };

  return (
    <div className="border border-white/10 bg-white/[0.03] rounded-xl p-3 space-y-2">
      <p className="text-sm font-semibold">🔐 รหัส PIN (ใช้ปลดล็อกหน้าเว็บ)</p>
      {statusLoading ? (
        <p className="text-xs text-slate-500 animate-pulse">กำลังเช็คสถานะ PIN...</p>
      ) : status?.pin_set ? (
        <p className="text-xs text-profit">ตั้ง PIN ไว้แล้ว — ทุกครั้งที่เปิดเว็บจะขอ PIN ก่อน (ผิด {status.max_failed} ครั้งติด → ล็อก {status.lock_minutes} นาที)</p>
      ) : statusErr ? (
        <p className="text-xs text-amber-400">เช็คสถานะ PIN ไม่สำเร็จ (API อาจกำลังเริ่มต้น) — รีเฟรชหน้าเพื่อลองอีกครั้ง</p>
      ) : (
        <p className="text-xs text-amber-400">ยังไม่ได้ตั้ง PIN — หน้าเว็บเปิดให้ใช้โดยไม่ต้องยืนยัน แนะนำให้ตั้งเพื่อความปลอดภัย</p>
      )}
      {/* flex-wrap + fluid inputs: fixed w-36 x2 + 2 buttons forced ~478px min-content,
          stretching every panel on this page past the 390px mobile viewport */}
      <div className="flex flex-wrap gap-2">
        <input
          type="password" inputMode="numeric" maxLength={6} value={pin}
          onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 6))}
          placeholder="PIN ใหม่ 6 หลัก" disabled={busy}
          className="min-w-0 flex-1 sm:flex-none sm:w-36 bg-surface border border-slate-700 rounded px-3 py-2 text-center tracking-widest"
        />
        <input
          type="password" inputMode="numeric" maxLength={6} value={confirm}
          onChange={(e) => setConfirm(e.target.value.replace(/\D/g, "").slice(0, 6))}
          placeholder="ยืนยัน PIN" disabled={busy}
          className="min-w-0 flex-1 sm:flex-none sm:w-36 bg-surface border border-slate-700 rounded px-3 py-2 text-center tracking-widest"
        />
        <button onClick={submit} disabled={busy}
          className="bg-accent text-surface font-semibold rounded px-4 py-2 text-sm disabled:opacity-50">
          {busy ? "..." : status?.pin_set ? "เปลี่ยน PIN" : "ตั้ง PIN"}
        </button>
        {status?.pin_set && (
          <button onClick={logout} disabled={busy}
            className="border border-slate-700 rounded px-3 py-2 text-sm text-slate-400 hover:text-loss">
            ออกจากระบบ
          </button>
        )}
      </div>
      {msg && <p className={`text-xs ${ok ? "text-profit" : "text-loss"}`}>{msg}</p>}
    </div>
  );
}
