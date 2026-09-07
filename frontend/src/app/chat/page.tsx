"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "@/lib/types";
import { clearToken, getToken, notifyAuthExpired } from "@/lib/auth";
import { CHAT_HISTORY_MAX, ChatMsg, loadChatHistory, saveChatHistory } from "@/lib/chat_history";

const SUGGESTIONS = [
  "วันนี้ควรเทรดไหม",
  "ตลาดทองน่าสนใจไหม",
  "ทำไม AI ไม่เปิดออเดอร์",
  "เดือนนี้ถึงเป้ากำไรไหม",
  "ควรลดความเสี่ยงไหม",
  "เป้าหมายกำไรสมเหตุสมผลไหม",
];

export default function ChatPage() {
  // ประวัติคงอยู่ข้ามการปิด/เปิดหน้า — โหลดจาก localStorage (เครื่องนี้) ตอน mount
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [thinkSecs, setThinkSecs] = useState(0);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setMessages(loadChatHistory());
  }, []);

  // live "thinking" counter + เลื่อนลงล่างเมื่อข้อความใหม่มา
  useEffect(() => {
    if (!loading) { setThinkSecs(0); return; }
    const t = setInterval(() => setThinkSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [loading]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const send = async (text: string) => {
    if (!text.trim() || loading) return;
    const next: ChatMsg[] = [...messages, { role: "user" as const, content: text }]
      .slice(-CHAT_HISTORY_MAX); // เก็บไม้ล่าสุด 20 ข้อความ
    setMessages(next);
    saveChatHistory(next);
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
        // context ที่ส่งให้ AI ก็จำกัด 20 ข้อความล่าสุดเท่ากัน
        body: JSON.stringify({ messages: next.slice(-CHAT_HISTORY_MAX) }),
      });
      // 401 = session token หมดอายุ — ล็อกกลับไปหน้า PIN แบบเดียวกับ
      // api.ts (handle401) ดู ChatWidget.tsx สำหรับรายละเอียดบั๊ก
      if (res.status === 401) {
        clearToken();
        notifyAuthExpired();
        throw new Error("หมดเวลาเข้าใช้งาน — ใส่ PIN อีกครั้งเพื่อใช้แชทต่อ");
      }
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        acc += decoder.decode(value, { stream: true });
        setMessages([...next, { role: "assistant", content: acc }]);
      }
      const finalMsgs: ChatMsg[] = [...next, { role: "assistant", content: acc || "(no reply)" }];
      setMessages(finalMsgs);
      saveChatHistory(finalMsgs);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      const content = msg.startsWith("หมดเวลา") ? msg : `เชื่อมต่อ AI ไม่ได้: ${msg}`;
      const errMsgs: ChatMsg[] = [...next, { role: "assistant", content }];
      setMessages(errMsgs);
      saveChatHistory(errMsgs);
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
              <div className={`max-w-[80%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap ${
                m.role === "user" ? "bg-accent text-white" : "bg-white/[0.06] border border-white/10"
              }`} >
                {m.content}
              </div>
            </div>
          ))}
          {loading && (
            <div className="flex items-center gap-2 text-slate-400 text-sm animate-pulse">
              <span className="inline-flex gap-1" aria-hidden="true">
                <span className="w-2 h-2 rounded-full bg-accent animate-bounce [animation-delay:0ms]" />
                <span className="w-2 h-2 rounded-full bg-accent animate-bounce [animation-delay:150ms]" />
                <span className="w-2 h-2 rounded-full bg-accent animate-bounce [animation-delay:300ms]" />
              </span>
              AI กำลังคิด... ({thinkSecs}s)
            </div>
          )}
          <div ref={endRef} />
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
            className="bg-accent text-white font-semibold rounded px-4 text-sm disabled:opacity-50">
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
