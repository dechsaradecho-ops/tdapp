"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "@/lib/types";

interface Msg { role: "user" | "assistant"; content: string }

const SUGGESTIONS = [
  "วันนี้ควรเทรดไหม",
  "ตลาดทองน่าสนใจไหม",
  "ทำไม AI ไม่เปิดออเดอร์",
  "ควรลดความเสี่ยงไหม",
];

/**
 * Floating AI chat popup (bottom-right). Mounted globally in layout.tsx so
 * the assistant is reachable from every page without navigating to /chat.
 */
export default function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading, open]);

  const send = async (text: string) => {
    if (!text.trim() || loading) return;
    const next: Msg[] = [...messages, { role: "user", content: text }];
    setMessages(next);
    setInput("");
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: next }),
      });
      const data = await res.json();
      setMessages([...next, { role: "assistant", content: data.reply ?? "(no reply)" }]);
    } catch (e) {
      setMessages([...next, { role: "assistant", content: `⚠️ เชื่อมต่อ AI ไม่ได้: ${e}` }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      {open && (
        <div className="fixed bottom-24 right-6 z-50 w-[360px] max-w-[calc(100vw-3rem)] h-[520px] max-h-[70vh] panel flex flex-col shadow-2xl">
          <div className="flex items-center justify-between pb-2 border-b border-slate-800">
            <p className="text-sm font-semibold">💬 AI Assistant</p>
            <button onClick={() => setOpen(false)}
              className="text-slate-500 hover:text-slate-300 text-lg leading-none"
              aria-label="ปิดแชท">✕</button>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto py-3">
            {!messages.length && (
              <p className="text-slate-500 text-xs">
                ถาม AI ได้ทุกเรื่องเกี่ยวกับตลาด/ความเสี่ยง/เป้าหมาย —
                ทุกคำตอบอิง Market Condition, Risk Analysis และ Portfolio Status
              </p>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                <div className={`max-w-[85%] rounded-lg px-3 py-2 text-xs whitespace-pre-wrap ${
                  m.role === "user" ? "bg-accent text-surface" : "bg-surface border border-slate-800"
                }`}>
                  {m.content}
                </div>
              </div>
            ))}
            {loading && <p className="text-slate-500 text-xs">AI กำลังคิด...</p>}
            <div ref={endRef} />
          </div>

          {!messages.length && (
            <div className="flex flex-wrap gap-1 pb-2">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => send(s)}
                  className="text-[11px] border border-slate-700 rounded-full px-2 py-0.5 text-slate-400 hover:border-accent hover:text-accent">
                  {s}
                </button>
              ))}
            </div>
          )}

          <div className="flex gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send(input)}
              placeholder="พิมพ์คำถาม..."
              className="flex-1 bg-surface border border-slate-700 rounded px-3 py-2 text-xs"
            />
            <button onClick={() => send(input)} disabled={loading}
              className="bg-accent text-surface font-semibold rounded px-3 text-xs disabled:opacity-50">
              ส่ง
            </button>
          </div>
        </div>
      )}

      <button
        onClick={() => setOpen((v) => !v)}
        aria-label="เปิดแชท AI"
        className="fixed bottom-6 right-6 z-50 w-14 h-14 rounded-full bg-accent text-surface text-2xl shadow-lg hover:scale-105 transition-transform flex items-center justify-center"
      >
        {open ? "✕" : "💬"}
      </button>
    </>
  );
}
