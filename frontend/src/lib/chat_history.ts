"use client";

/**
 * ประวัติแชท AI — เก็บในเครื่องนี้ (localStorage key: tdapp_chat_history)
 * เป็น UI state ของเครื่อง (เหมือน wallpaper) ไม่ใช่ข้อมูลบัญชี จึงไม่ย้ายลง DB
 * เก็บสูงสุด 20 ข้อความล่าสุด (user + assistant นับรวม) — ทั้งที่แสดงบนจอ
 * และที่ส่งเป็น context ให้ AI (กัน payload โตเกินจำเป็น)
 */
export type ChatMsg = { role: "user" | "assistant"; content: string };

export const CHAT_HISTORY_KEY = "tdapp_chat_history";
export const CHAT_HISTORY_MAX = 20;

export function loadChatHistory(): ChatMsg[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(CHAT_HISTORY_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return [];
    return arr
      .filter((m): m is ChatMsg =>
        m && (m.role === "user" || m.role === "assistant") &&
        typeof m.content === "string" && m.content.length > 0)
      .slice(-CHAT_HISTORY_MAX);
  } catch {
    return []; // ข้อมูลเสีย/JSON พัง — เริ่มแชทใหม่ ไม่ทำให้หน้า crash
  }
}

export function saveChatHistory(msgs: ChatMsg[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      CHAT_HISTORY_KEY,
      JSON.stringify(msgs.slice(-CHAT_HISTORY_MAX)));
  } catch {
    /* quota เกิน — ข้าม ประวัติอยู่ใน state จนปิดแท็บ */
  }
}
