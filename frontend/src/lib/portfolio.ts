"use client";

/**
 * Portfolio store กลาง — Capital / PnL / Equity จาก DB (single source of truth)
 *
 * ย้ายจาก localStorage (2026-09-06): ตัวเลขทุกตัวคำนวณฝั่ง backend จาก
 * paper_trades + trading_settings แล้วส่งมาทาง /api/trading/monitor
 * (equity / pnl fields) — ทุกหน้า/ทุกเครื่องเห็นค่าเดียวกัน และปุ่ม
 * รีเซ็ตสถิติบนหน้า monitor รีเซ็ต Current Equity / Current PnL ได้จริง
 * เพราะลบไม้ที่ปิดแล้ว + เคลียร์ equity_snapshots ที่ต้นทาง
 *
 * - capital = settings.capital (ทุนตั้งต้นที่ผู้ใช้ตั้ง)
 * - pnl     = realized (ไม้ปิดแล้ว) + unrealized (ไม้ค้าง mark ราคาปัจจุบัน)
 * - equity  = capital + pnl
 * - setCapital ยิง PUT /api/settings (บันทึกลง DB ทันที)
 */
import { useSyncExternalStore } from "react";
import { api } from "@/lib/api";

export type PortfolioState = {
  capital: number;
  pnl: number;
  equity: number;
  loaded: boolean; // false = ยังไม่ได้ดึง /monitor ครั้งแรก (แสดงค่าเริ่มต้น)
};

// getServerSnapshot ต้องคืน object เดิมเสมอ (React บังคับ) — สร้างครั้งเดียว
const SSR_SNAPSHOT: PortfolioState = {
  capital: 0, pnl: 0, equity: 0, loaded: false,
};

let state: PortfolioState = { ...SSR_SNAPSHOT };
const listeners = new Set<() => void>();

function emit(next: Partial<PortfolioState>) {
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

/** ดึงตัวเลขจริงจาก backend (monitor snapshot) — เรียกซ้ำได้ทุกหน้า */
export async function refreshPortfolio(): Promise<void> {
  try {
    const snap = await api.monitor();
    emit({
      capital: snap.capital,
      pnl: snap.pnl,
      equity: snap.equity,
      loaded: true,
    });
  } catch {
    /* backend หลับ/ยังไม่ login — คงค่าเดิมไว้ ไม่ทำให้หน้าพัง */
  }
}

/** บันทึกทุนตั้งต้นลง DB (settings.capital) แล้วอัปเดต store ทันที */
export async function saveCapitalToDb(v: number): Promise<void> {
  if (!Number.isFinite(v) || v < 0) return;
  emit({ capital: v, equity: v + state.pnl });
  try {
    await api.saveSettings({ capital: v });
  } catch {
    /* save ล้มเหลว — ค่ายังอยู่ใน store จนกว่า refresh ถัดไป */
  }
}

export function usePortfolio() {
  const snapshot = useSyncExternalStore<PortfolioState>(
    (cb) => {
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
      };
    },
    () => state, // client snapshot
    () => SSR_SNAPSHOT // SSR snapshot — กัน hydration mismatch (object เดิม)
  );
  return {
    capital: snapshot.capital,
    pnl: snapshot.pnl,
    equity: snapshot.equity,
    loaded: snapshot.loaded,
    setCapital: saveCapitalToDb,
  };
}
