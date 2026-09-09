"use client";

import { useEffect, useRef, useState } from "react";
import Icon from "@/components/Icon";
import LoadingGraphic from "@/components/LoadingGraphic";
import { getToken, clearToken, notifyAuthExpired } from "@/lib/auth";
import { API_BASE, SignalProposal } from "@/lib/types";

/**
 * คำแนะนำจาก AI (หน้า signals) — สรุปสัญญาณที่กำลังราวด์ทั้งหมดเป็นภาษาคน
 * ใช้ endpoint /api/chat/stream เดิม (context = ตารางสัญญาณจริงจาก scanner)
 * stream แบบเดียวกับ ChatWidget (typing effect) — กดปุ่มเพื่อขอใหม่ได้
 */
export default function AiAdvicePanel({ signals }: { signals: SignalProposal[] }) {
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [thinkSecs, setThinkSecs] = useState(0);
  const [error, setError] = useState("");
  const runId = useRef(0);

  // live "thinking" counter — เหมือน ChatWidget (โมเดลใช้เวลา 10-60 วิ)
  useEffect(() => {
    if (!loading) { setThinkSecs(0); return; }
    const t = setInterval(() => setThinkSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [loading]);

  const ask = async () => {
    if (loading) return;
    const id = ++runId.current;
    setLoading(true);
    setError("");
    setText("");
    try {
      const token = getToken();
      // context: สรุปสัญญาณที่แสดงบนหน้าตอนนี้ (pending ก่อน แล้ว approved)
      const lines = signals.map((s) =>
        `- ${s.asset} ${s.direction} @ ${s.entry} | conf ${s.confidence}% | `
        + `SL ${s.stop_loss} TP ${s.take_profit} RR ${s.expected_rr} | `
        + `${s.approval === "approved" ? "อนุมัติแล้ว" : "รออนุมัติ"}`
        + (s.reason?.length ? ` | เหตุผล: ${s.reason.slice(0, 3).join("; ")}` : ""));
      const prompt = signals.length
        ? "สรุปสัญญาณทั้งหมดนี้เป็นคำแนะนำสั้น ๆ สำหรับวันนี้ (ภาษาไทย, ไม่เกิน 8 บรรทัด): "
          + "ไม้ไหนน่าสนใจที่สุด เสี่ยงอะไรไหม และควรระวังอะไร ตอบตรงประเด็นไม่ต้องทักทาย\n"
          + lines.join("\n")
        : "ตอนนี้ยังไม่มีสัญญาณในระบบ — แนะนำสั้น ๆ ว่าควรทำอะไรดีระหว่างรอสัญญาณ (ภาษาไทย ไม่เกิน 4 บรรทัด ไม่ต้องทักทาย)";
      const res = await fetch(`${API_BASE}/api/chat/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          messages: [{ role: "user", content: prompt }],
        }),
      });
      if (res.status === 401) {
        clearToken();
        notifyAuthExpired();
        throw new Error("หมดเวลาเข้าใช้งาน — ใส่ PIN อีกครั้งเพื่อใช้ต่อ");
      }
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = "";
      // typing effect เดียวกับ ChatWidget — ตัวจริงรันใน interval กัน burst chunks
      let shown = 0;
      const typer = setInterval(() => {
        if (runId.current !== id) return;
        if (shown < acc.length) {
          shown = Math.min(acc.length, shown + 6);
          setText(acc.slice(0, shown));
        }
      }, 16);
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          acc += decoder.decode(value, { stream: true });
        }
      } finally {
        clearInterval(typer);
        if (runId.current === id) setText(acc || "(ไม่มีข้อความตอบกลับ)");
      }
    } catch (e) {
      if (runId.current === id) {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      if (runId.current === id) setLoading(false);
    }
  };

  return (
    <div className="panel">
      <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
        <h2 className="panel-title flex items-center gap-1.5">
          <Icon n="bulb" size={16} className="text-accent" /> คำแนะนำจาก AI
        </h2>
        <button
          onClick={ask}
          disabled={loading}
          aria-busy={loading}
          className="border border-slate-700 bg-white/[0.05] text-slate-200 rounded px-3 py-2 text-xs min-h-[36px] font-semibold active:bg-white/10 disabled:opacity-50"
        >
          <span className="inline-flex items-center gap-1.5">
            {loading && <Icon n="spinner" size={13} className="animate-spin" />}
            {loading ? `กำลังคิด... ${thinkSecs}s` : text ? "ขอคำแนะนำใหม่" : "ขอคำแนะนำ"}
          </span>
        </button>
      </div>
      {error && <p className="text-amber-400 text-sm">{error}</p>}
      {!text && !loading && !error && (
        <p className="text-slate-500 text-sm">
          กด &quot;ขอคำแนะนำ&quot; ให้ AI สรุปสัญญาณทั้งหมดตอนนี้ ({signals.length} รายการ) เป็นภาษาคน
        </p>
      )}
      {loading && !text && (
        <LoadingGraphic message={`AI กำลังอ่านสัญญาณ... (${thinkSecs}s — อาจใช้ 10–60 วิ)`} compact />
      )}
      {text && (
        <p className="text-sm whitespace-pre-wrap leading-relaxed text-slate-200">{text}</p>
      )}
    </div>
  );
}
