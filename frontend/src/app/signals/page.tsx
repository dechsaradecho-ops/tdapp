"use client";

import { useEffect, useState } from "react";
import SignalCard from "@/components/SignalCard";
import { api } from "@/lib/api";
import { SignalProposal } from "@/lib/types";

export default function SignalsPage() {
  const [signals, setSignals] = useState<SignalProposal[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.latestSignals().then(setSignals).catch((e) => setError(String(e)));
  }, []);

  return (
    <div className="space-y-4">
      <h2 className="panel-title">SEMI-AUTO — ข้อเสนอการเทรด (รอการอนุมัติ)</h2>
      {error && <p className="text-loss text-sm">{error}</p>}
      {!signals.length && !error && <p className="text-slate-500 text-sm">ยังไม่มีสัญญาณ — รอ Market Scanner</p>}
      <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
        {signals.map((s) => <SignalCard key={s.asset} signal={s} />)}
      </div>
    </div>
  );
}
