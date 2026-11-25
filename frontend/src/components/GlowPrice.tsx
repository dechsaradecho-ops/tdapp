"use client";

import { useEffect, useRef, useState } from "react";
import { fmtNum } from "@/lib/format";

/** Freshness cutoff (seconds) — mirrors backend `_DISPLAY_FRESH_AGE`
 *  (quotes.py): open + !stale + age ≤ 10s = สด. */
export const FRESH_AGE_S = 10;

export type QuoteFreshness = "fresh" | "delayed" | "closed";

/** Display-tick sources that are NOT live quotes — daily close / demo /
 *  plan fallbacks never glow (monitor keeps its legacy mark vocabulary
 *  "spot"|"daily"|"broker"|"entry"; signals carry the display source
 *  "biquote"|"yahoo_fallback"|"yahoo"|"daily"|"demo"). */
const STALE_SOURCES = new Set(["daily", "demo", "entry", "broker"]);

export interface FreshnessInput {
  ageS?: number | null;
  marketState?: string | null;
  source?: string | null;
  /** Explicit stale flag (e.g. monitor: price_source !== "spot"). */
  stale?: boolean | null;
}

/** Shared สด/ดีเลย์/ปิดตลาด classifier (Global Constraints, verbatim):
 *  สด = open + !stale + age ≤ 10s; ดีเลย์ = open but stale or age > 10s;
 *  ปิดตลาด = closed. Unknown/old payloads (no age, missing or unexpected
 *  marketState) → ดีเลย์, never สด. */
export function quoteFreshness(input: FreshnessInput): QuoteFreshness {
  const { ageS, marketState, source, stale } = input;
  const state = (marketState ?? "").toLowerCase();
  if (state === "closed") return "closed";
  // Require "open" explicitly — null/""/unexpected states can never be fresh,
  // so a payload missing market_state never glows even with a valid age.
  if (state !== "open") return "delayed";
  const src = (source ?? "").toLowerCase();
  if (stale || STALE_SOURCES.has(src) || src.includes("demo")) return "delayed";
  if (ageS == null || !Number.isFinite(ageS) || ageS < 0) return "delayed";
  return ageS <= FRESH_AGE_S ? "fresh" : "delayed";
}

export function isFreshQuote(input: FreshnessInput): boolean {
  return quoteFreshness(input) === "fresh";
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

/** Tracks the rendered-value delta and returns the latest tick
 *  ({ dir, gen }) for ~1s after a change while `fresh` — null otherwise
 *  (static, no glow on stale/demo/closed/unknown-state). `gen` increments on
 *  every tick so a same-direction re-tick inside the ~1s window still
 *  re-triggers the flash (the span keys on it — React `setState` with an
 *  identical "up"/"down" value would bail out and the keyframe would never
 *  restart). Timer cleanup on unmount + on each new tick. */
export function useGlowPrice(value: number | null | undefined, fresh: boolean) {
  const [tick, setTick] = useState<{ dir: "up" | "down"; gen: number } | null>(null);
  const prevRef = useRef<number | null>(null);
  const genRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const v = typeof value === "number" && Number.isFinite(value) ? value : null;
    const prev = prevRef.current;
    prevRef.current = v;
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (prev == null || v == null || v === prev || !fresh) {
      setTick(null);
      return;
    }
    genRef.current += 1;
    const gen = genRef.current;
    setTick({ dir: v > prev ? "up" : "down", gen });
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      // Only clear our own tick — a newer tick (higher gen) owns the flash now.
      setTick((cur) => (cur && cur.gen === gen ? null : cur));
    }, 1000);
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [value, fresh]);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  return tick;
}

export interface GlowPriceProps extends FreshnessInput {
  value: number | null | undefined;
  decimals?: number;
  className?: string;
  title?: string;
}

/** Live price with directional glow: on `value` change while fresh
 *  (open + !stale + age ≤ 10s) flashes green (up) / red (down) fading ~1s;
 *  static otherwise. `prefers-reduced-motion` → plain color change. */
export function GlowPrice({
  value,
  ageS,
  source,
  marketState,
  stale,
  decimals = 5,
  className = "",
  title,
}: GlowPriceProps) {
  const fresh = isFreshQuote({ ageS, marketState, source, stale });
  const tick = useGlowPrice(value, fresh);
  const reduced = useReducedMotion();
  const text = value == null || !Number.isFinite(value) ? "—" : fmtNum(value, decimals);

  let glowClass = "";
  if (tick) {
    glowClass = reduced
      ? (tick.dir === "up" ? "text-profit" : "text-loss")
      : (tick.dir === "up" ? "glow-up" : "glow-down");
  }
  return (
    <span
      // Key on the tick generation so a same-direction re-tick remounts the
      // span and restarts the CSS keyframe (same class re-applied alone will
      // not replay the animation).
      key={tick ? `${tick.dir}-${tick.gen}` : "static"}
      className={`${className} ${glowClass}`.trim()}
      title={title}
      aria-live="off"
    >
      {text}
    </span>
  );
}

export interface FreshnessBadgeProps extends FreshnessInput {
  className?: string;
}

/** สด / ดีเลย์ / ปิดตลาด badge per the Global Constraints cutoffs. */
export function FreshnessBadge({
  ageS,
  marketState,
  source,
  stale,
  className = "",
}: FreshnessBadgeProps) {
  const f = quoteFreshness({ ageS, marketState, source, stale });
  if (f === "fresh") {
    return (
      <span
        className={`inline-flex shrink-0 items-center rounded-full bg-profit/20 px-1.5 py-0.5 text-[10px] font-semibold text-profit ${className}`.trim()}
        title={ageS != null ? `ราคาสด — อายุ ${ageS.toFixed(1)} วิ` : "ราคาสด"}
      >
        สด
      </span>
    );
  }
  if (f === "closed") {
    return (
      <span
        className={`inline-flex shrink-0 items-center rounded-full bg-slate-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-slate-300 ${className}`.trim()}
        title="ตลาดปิด — ราคานิ่ง"
      >
        ปิดตลาด
      </span>
    );
  }
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-full bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-amber-400 ${className}`.trim()}
      title={ageS != null ? `ราคาดีเลย์ — อายุ ${ageS.toFixed(1)} วิ` : "ราคาดีเลย์ — อาจไม่อัปเดต"}
    >
      ดีเลย์
    </span>
  );
}
