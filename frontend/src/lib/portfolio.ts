"use client";

/**
 * Portfolio store กลาง — Capital/PnL ที่ผู้ใช้ตั้งค่า
 * - เก็บใน localStorage (tdapp_portfolio) → ปิดเบราว์เซอร์แล้วค่าไม่หาย
 * - ทุกหน้า/คอมโพเนนต์ที่ใช้ usePortfolio() จะ sync กันทันที (subscribe pattern)
 */
import { useSyncExternalStore } from "react";

const KEY = "tdapp_portfolio";
const DEFAULT_CAPITAL = 100000;
const DEFAULT_PNL = 1200;
const DEFAULTS: PortfolioState = { capital: DEFAULT_CAPITAL, pnl: DEFAULT_PNL };
// getServerSnapshot ต้องคืน object เดิมเสมอ (React บังคับ) — สร้างครั้งเดียว
const SSR_SNAPSHOT: PortfolioState = { ...DEFAULTS };

export type PortfolioState = { capital: number; pnl: number };

function load(): PortfolioState {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    const p = JSON.parse(raw) as Partial<PortfolioState>;
    return {
      capital: Number.isFinite(Number(p.capital)) && Number(p.capital) > 0 ? Number(p.capital) : DEFAULTS.capital,
      pnl: Number.isFinite(Number(p.pnl)) ? Number(p.pnl) : DEFAULTS.pnl,
    };
  } catch {
    return { ...DEFAULTS };
  }
}

// null = ยังไม่เคยโหลดจาก localStorage (โหลดครั้งแรกตอน getSnapshot ถูกเรียกบน client)
let state: PortfolioState | null = null;
const listeners = new Set<() => void>();

function getState(): PortfolioState {
  if (!state) state = load();
  return state;
}

function persist() {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* localStorage เต็ม/ถูกปิด — ข้ามได้ */
  }
}

export function setPortfolioCapital(v: number) {
  if (Number.isFinite(v) && v >= 0) {
    state = { ...getState(), capital: v };
    persist();
    listeners.forEach((l) => l());
  }
}

export function usePortfolio() {
  const snapshot = useSyncExternalStore<PortfolioState>(
    (cb) => {
      // sync ข้ามแท็บด้วย storage event
      const onStorage = (e: StorageEvent) => {
        if (e.key === KEY) {
          state = load();
          cb();
        }
      };
      window.addEventListener("storage", onStorage);
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
        window.removeEventListener("storage", onStorage);
      };
    },
    () => getState(), // client snapshot — lazy-load จาก localStorage ครั้งแรก
    () => SSR_SNAPSHOT // SSR snapshot — กัน hydration mismatch (ต้องเป็น object เดิม)
  );
  return {
    capital: snapshot.capital,
    pnl: snapshot.pnl,
    equity: snapshot.capital + snapshot.pnl,
    setCapital: setPortfolioCapital,
  };
}
