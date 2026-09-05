"use client";

import { useState } from "react";

/**
 * จัดหมวดหมู่เหตุผล (reason) ของ signal card — แต่ละหมวด toggle พับ/กางได้
 * หมวดอิงจาก keyword ที่ StrategyEngine ใช้เขียน reason (ภาษาไทย) จริง:
 *   trend     — EMA / ADX / เทรนด์ / Supertrend
 *   momentum  — RSI / MACD / โมเมนตัม
 *   volatility— ATR / ผันผวน / volatility
 *   news      — ข่าว / impact / sentiment
 *   score     — Opportunity Score (หัวข้อแรกที่ build_proposal ใส่)
 *   other     — ที่เหลือ
 */
type Category = "score" | "trend" | "momentum" | "volatility" | "news" | "other";

const CATEGORY_META: Record<Category, { label: string; icon: string; color: string }> = {
  score: { label: "คะแนนโอกาส", icon: "🎯", color: "text-accent" },
  trend: { label: "เทรนด์", icon: "📈", color: "text-profit" },
  momentum: { label: "โมเมนตัม", icon: "⚡", color: "text-amber-400" },
  volatility: { label: "ความผันผวน", icon: "🌊", color: "text-sky-400" },
  news: { label: "ข่าว/เซนติเมนต์", icon: "📰", color: "text-purple-400" },
  other: { label: "อื่นๆ", icon: "•", color: "text-slate-400" },
};

const CATEGORY_ORDER: Category[] = ["score", "trend", "momentum", "volatility", "news", "other"];

function classify(reason: string): Category {
  const r = reason.toLowerCase();
  if (r.includes("opportunity score")) return "score";
  if (r.includes("ema") || r.includes("adx") || r.includes("เทรนด์") || r.includes("supertrend")) return "trend";
  if (r.includes("rsi") || r.includes("macd") || r.includes("โมเมนตัม")) return "momentum";
  if (r.includes("atr") || r.includes("ผันผวน") || r.includes("volatility")) return "volatility";
  if (r.includes("ข่าว") || r.includes("impact") || r.includes("sentiment") || r.includes("เซนติเมนต์")) return "news";
  return "other";
}

export default function ReasonList({ reasons }: { reasons: string[] }) {
  // เปิดหมวดที่มีเหตุผลไว้ตั้งแต่แรก (score มักมี 1 ข้อ) — ปิดหมวดว่าง
  const [open, setOpen] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    for (const c of CATEGORY_ORDER) init[c] = false;
    return init;
  });

  const groups = CATEGORY_ORDER
    .map((cat) => ({ cat, items: reasons.filter((r) => classify(r) === cat) }))
    .filter((g) => g.items.length > 0);

  if (!groups.length) return null;

  const toggle = (cat: Category) => setOpen((prev) => ({ ...prev, [cat]: !prev[cat] }));

  return (
    <div className="mt-2 space-y-1">
      {groups.map(({ cat, items }) => (
        <div key={cat} className="rounded-xl border border-white/10 bg-white/[0.04]">
          <button
            onClick={() => toggle(cat)}
            className="flex w-full items-center justify-between px-2 py-1.5 text-xs hover:bg-white/10 rounded-xl"
            aria-expanded={open[cat]}
          >
            <span className={`flex items-center gap-1.5 font-semibold ${CATEGORY_META[cat].color}`}>
              <span>{CATEGORY_META[cat].icon}</span>
              {CATEGORY_META[cat].label}
              <span className="text-slate-500">({items.length})</span>
            </span>
            <span className="text-slate-500">{open[cat] ? "▾" : "▸"}</span>
          </button>
          {open[cat] && (
            <ol className="list-decimal list-inside px-3 pb-2 space-y-1 text-slate-300 text-xs">
              {items.map((r, i) => <li key={i}>{r}</li>)}
            </ol>
          )}
        </div>
      ))}
    </div>
  );
}
