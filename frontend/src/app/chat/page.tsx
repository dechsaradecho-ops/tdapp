"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/types";

interface Msg { role: "user" | "assistant"; content: string }

const SUGGESTIONS = [
  "วันนี้ควรเทรดไหม",
  "ตลาดทองน่าสนใจไหม",
  "ทำไม AI ไม่เปิดออเดอร์",
  "เดือนนี้ถึงเป้ากำไรไหม",
  "ควรลดความเสี่ยงไหม",
  "เป้าหมายกำไรสมเหตุสมผลไหม",
];

export default function ChatPage() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);

  const send = async (text: string) => {
    if (!text.trim() || loading) return;
    const next: Msg[] = [...messages, { role: "user", content: text }];
    setMessages(next);
    setInput("");
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: next }),
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      setMessages([...next, { role: "assistant", content: "" }]);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        acc += decoder.decode(value, { stream: true });
        setMessages([...next, { role: "assistant", content: acc }]);
      }
      if (!acc.trim()) {
        setMessages([...next, { role: "assistant", content: "(no reply)" }]);
      }
    } catch (e) {
      setMessages([...next, { role: "assistant", content: `⚠️ เชื่อมต่อ AI ไม่ได้: ${e}` }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-4">
      <div className="panel min-h-[400px] flex flex-col">
        <div className="flex-1 space-y-3 overflow-y-auto max-h-[500px]">
          {!messages.length && (
            <p className="text-slate-500 text-sm">
              ถาม AI ได้ทุกเรื่องเกี่ยวกับตลาด/ความเสี่ยง/เป้าหมาย — ทุกคำตอบอิง Market Condition,
              Risk Analysis, Opportunity Score และ Portfolio Status
            </p>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
              <div className={`max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
                m.role === "user" ? "bg-accent text-surface" : "bg-surface border border-slate-800"
              }`}>
                {m.content}
              </div>
            </div>
          ))}
          {loading && <p className="text-slate-500 text-sm">AI กำลังคิด...</p>}
        </div>
        <div className="mt-3 flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send(input)}
            placeholder="พิมพ์คำถาม..."
            className="flex-1 bg-surface border border-slate-700 rounded px-3 py-2 text-sm"
          />
          <button onClick={() => send(input)} disabled={loading}
            className="bg-accent text-surface font-semibold rounded px-4 text-sm disabled:opacity-50">
            ส่ง
          </button>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {SUGGESTIONS.map((s) => (
          <button key={s} onClick={() => send(s)}
            className="text-xs border border-slate-700 rounded-full px-3 py-1 text-slate-400 hover:border-accent hover:text-accent">
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}
