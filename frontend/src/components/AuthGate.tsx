"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { AUTH_EXPIRED_EVENT, clearToken, getToken, setToken } from "@/lib/auth";
import { PinStatus } from "@/lib/types";

/** Full-screen PIN gate — renders children only after a valid session.
 *
 * - No PIN configured yet → children render immediately (bootstrap mode);
 *   setup UI lives on /settings.
 * - Locked out → shows a countdown; input disabled until the window ends.
 */
export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [checking, setChecking] = useState(true);
  const [status, setStatus] = useState<PinStatus | null>(null);
  const [pin, setPin] = useState("");
  const [msg, setMsg] = useState("");
  const [msgType, setMsgType] = useState<"error" | "ok">("error");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());
  const inputRef = useRef<HTMLInputElement>(null);

  const loadStatus = useCallback(async () => {
    try {
      const st = await api.authStatus();
      setStatus(st);
      return st;
    } catch {
      setStatus(null);
      return null;
    }
  }, []);

  // On mount: if we hold a token, try a cheap authenticated call to validate it.
  useEffect(() => {
    (async () => {
      const st = await loadStatus();
      if (st?.pin_set && getToken()) {
        try {
          // Cheap authenticated probe with a hard timeout so a slow/hanging
          // API can never leave the gate stuck on the "checking" screen.
          await Promise.race([
            api.dbCounts(),
            new Promise<never>((_, rej) =>
              setTimeout(() => rej(new Error("probe timeout")), 15000)),
          ]);
          setChecking(false);       // token still valid → straight in
          return;
        } catch {
          clearToken();             // dead token / timeout → show PIN pad
        }
      }
      setChecking(false);
    })();
  }, [loadStatus]);

  // React to 401s fired anywhere in the app + tick the lockout countdown.
  useEffect(() => {
    const onExpired = () => {
      clearToken();
      setMsg("เซสชันหมดอายุ — กรอก PIN อีกครั้ง");
      setMsgType("error");
      loadStatus();
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
      clearInterval(t);
    };
  }, [loadStatus]);

  useEffect(() => {
    if (!checking && status?.pin_set && !status.locked) inputRef.current?.focus();
  }, [checking, status]);

  const submitPin = async (value: string) => {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.authLogin(value);
      if (r.ok && r.token) {
        setToken(r.token);
        setPin("");
        setMsg("");
        await loadStatus();
        window.location.reload();     // remount everything with the token in place
      } else {
        setMsg(r.message || "PIN ไม่ถูกต้อง");
        setMsgType("error");
        setPin("");
        await loadStatus();
      }
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
      setMsgType("error");
      setPin("");
    } finally {
      setBusy(false);
    }
  };

  const press = (digit: string) => {
    if (busy || status?.locked) return;
    const next = (pin + digit).slice(0, 6);
    setPin(next);
    if (next.length === 6) submitPin(next);
  };

  const erase = () => setPin((p) => p.slice(0, -1));

  const lockRemainingSec = () => {
    if (!status?.locked || !status.locked_until) return 0;
    return Math.max(0, Math.floor((new Date(status.locked_until).getTime() - now) / 1000));
  };

  const remaining = lockRemainingSec();
  const mm = String(Math.floor(remaining / 60)).padStart(2, "0");
  const ss = String(remaining % 60).padStart(2, "0");

  if (checking) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/95">
        <p className="text-slate-400 animate-pulse">กำลังตรวจสอบเซสชัน...</p>
      </div>
    );
  }

  // Bootstrap mode: no PIN set → let the user in (they can set one on /settings).
  if (!status?.pin_set) return <>{children}</>;

  if (remaining > 0) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/95">
        <div className="panel p-8 text-center max-w-sm">
          <p className="text-4xl mb-2">🔒</p>
          <h2 className="panel-title">บัญชีถูกล็อกชั่วคราว</h2>
          <p className="text-loss font-bold text-xl mt-2">{mm}:{ss}</p>
          <p className="text-xs text-slate-500 mt-2">
            พยายามผิดครบ {status.max_failed} ครั้ง — ระบบจะเปิดให้กรอกใหม่อัตโนมัติ
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/95 p-4">
      <div className="panel p-8 w-full max-w-xs text-center">
        <p className="text-3xl mb-1">🔐</p>
        <h2 className="panel-title">กรอก PIN เข้าใช้งาน</h2>

        <input
          ref={inputRef}
          type="password"
          inputMode="numeric"
          maxLength={6}
          value={pin}
          disabled={busy}
          onChange={(e) => {
            const v = e.target.value.replace(/\D/g, "").slice(0, 6);
            setPin(v);
            if (v.length === 6) submitPin(v);
          }}
          onKeyDown={(e) => e.key === "Escape" && erase()}
          className="mt-4 w-full text-center text-3xl tracking-[0.6em] bg-surface border border-slate-700 rounded py-3 focus:border-accent outline-none"
          placeholder="••••••"
        />

        <div className="grid grid-cols-3 gap-2 mt-4">
          {["1", "2", "3", "4", "5", "6", "7", "8", "9"].map((d) => (
            <button key={d} onClick={() => press(d)} disabled={busy}
              className="bg-surface border border-slate-700 rounded py-3 text-lg hover:border-accent disabled:opacity-40">
              {d}
            </button>
          ))}
          <button onClick={erase} disabled={busy}
            className="bg-surface border border-slate-700 rounded py-3 hover:border-loss disabled:opacity-40">⌫</button>
          <button onClick={() => press("0")} disabled={busy}
            className="bg-surface border border-slate-700 rounded py-3 text-lg hover:border-accent disabled:opacity-40">0</button>
          <button onClick={() => setPin("")} disabled={busy}
            className="bg-surface border border-slate-700 rounded py-3 text-slate-400 hover:border-accent disabled:opacity-40">C</button>
        </div>

        {msg && (
          <p className={`text-sm mt-4 ${msgType === "error" ? "text-loss" : "text-profit"}`}>{msg}</p>
        )}
        {status.failed_attempts > 0 && (
          <p className="text-xs text-amber-400 mt-2">
            พิมพ์ผิด {status.failed_attempts}/{status.max_failed} ครั้ง — ครบ {status.max_failed} ครั้งจะล็อก {status.lock_minutes} นาที
          </p>
        )}
      </div>
    </div>
  );
}
