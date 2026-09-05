"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "@/lib/types";
import { getToken } from "@/lib/auth";

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
  const [thinkSecs, setThinkSecs] = useState(0);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading, open]);

  // live "thinking" counter so the UI keeps moving while the model
  // prepares its first token (can take 10-60s on complex questions)
  useEffect(() => {
    if (!loading) { setThinkSecs(0); return; }
    const t = setInterval(() => setThinkSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [loading]);

  const send = async (text: string) => {
    if (!text.trim() || loading) return;
    const next: Msg[] = [...messages, { role: "user", content: text }];
    setMessages(next);
    setInput("");
    setLoading(true);
    try {
      const token = getToken();
      const res = await fetch(`${API_BASE}/api/chat/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ messages: next }),
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

      // stream the reply: append chunks as they arrive (AI "typing" effect)
      setMessages([...next, { role: "assistant", content: "" }]);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = "";
      // reveal text gradually even when network chunks arrive in bursts —
      // gives a continuous typing feel instead of one big dump
      let shown = 0;
      const typer = setInterval(() => {
        if (shown < acc.length) {
          shown = Math.min(acc.length, shown + 6);
          setMessages([...next, { role: "assistant", content: acc.slice(0, shown) }]);
        }
      }, 16);
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          acc += decoder.decode(value, { stream: true });
        }
      } finally {
        // flush remaining text then stop the typer
        clearInterval(typer);
        setMessages([...next, { role: "assistant", content: acc || "(no reply)" }]);
      }
    } catch (e) {
      setMessages([...next, { role: "assistant", content: `⚠️ เชื่อมต่อ AI ไม่ได้: ${e}` }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      {open && (
        // มือถือ: เต็มจอ (sheet) — เดสก์ท็อป: กล่องลอยขวาล่างเหมือนเดิม
        <div className="fixed inset-0 z-50 sm:inset-auto sm:bottom-24 sm:right-6 sm:z-50 sm:w-[360px] sm:max-w-[calc(100vw-3rem)] sm:h-[520px] sm:max-h-[70vh] panel flex flex-col shadow-2xl rounded-none sm:rounded-xl safe-top animate-pop">
          <div className="flex items-center justify-between pb-2 border-b border-white/10">
            <p className="text-sm font-semibold">💬 AI Assistant</p>
            <button onClick={() => setOpen(false)}
              className="w-11 h-11 -mr-2 flex items-center justify-center text-slate-500 hover:text-slate-300 text-lg leading-none active:bg-white/10 rounded-lg"
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
                <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-xs whitespace-pre-wrap ${
                  m.role === "user" ? "bg-accent text-white" : "bg-white/[0.06] border border-white/10"
                }`}>
                  {m.content}
                </div>
              </div>
            ))}
            {loading && !messages[messages.length - 1]?.content && (
              <p className="text-slate-500 text-xs animate-pulse">
                💭 AI กำลังคิด... ({thinkSecs}s)
              </p>
            )}
            <div ref={endRef} />
          </div>

          {!messages.length && (
            <div className="flex flex-wrap gap-1 pb-2">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => send(s)}
                  className="text-[11px] border border-white/15 bg-white/[0.04] rounded-full px-2 py-0.5 text-slate-400 hover:border-accent hover:text-accent">
                  {s}
                </button>
              ))}
            </div>
          )}

          <div className="flex gap-2 pb-[env(safe-area-inset-bottom,0px)]">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send(input)}
              placeholder="พิมพ์คำถาม..."
              className="flex-1 bg-white/5 border border-white/15 rounded-xl px-3 py-2.5 text-sm min-h-[44px] focus:border-accent outline-none"
            />
            <button onClick={() => send(input)} disabled={loading}
              className="bg-accent text-white font-semibold rounded-xl px-4 min-h-[44px] text-sm disabled:opacity-50 active:brightness-90">
              ส่ง
            </button>
          </div>
        </div>
      )}

      <button
        onClick={() => setOpen((v) => !v)}
        aria-label="เปิดแชท AI"
        className="fixed bottom-6 right-4 sm:right-6 z-40 w-14 h-14 rounded-full bg-accent text-white text-2xl active:scale-95 sm:hover:scale-105 transition-transform flex items-center justify-center"
        style={{
          bottom: "calc(4.75rem + env(safe-area-inset-bottom, 0px))",
          boxShadow: "0 8px 28px rgba(10, 132, 255, 0.45), inset 0 1px 0 rgba(255,255,255,0.25)",
        }}
      >
        {open ? "✕" : "💬"}
      </button>
    </>
  );
}
