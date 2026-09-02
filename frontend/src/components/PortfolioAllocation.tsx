"use client";

import { AllocationItem } from "@/lib/types";

export default function PortfolioAllocation({ allocation }: { allocation: AllocationItem[] }) {
  const colors = ["bg-accent", "bg-emerald-500", "bg-amber-500", "bg-purple-500", "bg-slate-600"];
  let acc = 0;
  const segments = allocation.map((a, i) => {
    const start = acc;
    acc += a.weight_pct;
    return { ...a, start, color: colors[i % colors.length] };
  });

  return (
    <div>
      <div className="flex h-6 rounded overflow-hidden bg-slate-800">
        {segments.map((s) => (
          <div key={s.asset} className={s.color} style={{ width: `${s.weight_pct}%` }}
            title={`${s.asset} ${s.weight_pct}%`} />
        ))}
      </div>
      <ul className="mt-3 space-y-2 text-sm">
        {allocation.map((a, i) => (
          <li key={a.asset} className="flex items-start justify-between gap-3">
            <span className="flex items-center gap-2 min-w-0">
              <span className={`inline-block w-3 h-3 rounded ${segments[i].color}`} />
              <span className="font-semibold">{a.asset}</span>
              <span className="text-slate-500 truncate">{a.rationale}</span>
            </span>
            <span className="font-bold shrink-0">{a.weight_pct}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
