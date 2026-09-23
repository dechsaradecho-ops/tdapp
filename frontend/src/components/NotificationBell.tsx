"use client";

/**
 * NotificationBell — กระดิ่งแจ้งเตือนมุมขวาบนของ DesktopNav
 *
 * อ่านจาก GET /api/system/notifications (ตาราง `notifications` เดียวกับที่
 * LINE/Web-Push เขียนไว้). กดกระดิ่ง → popover แบบย่อ (ไอคอน + หัวข้อ + เวลา);
 * กดแถว → expand ดูข้อความเต็ม + ช่องทาง + สถานะ + เวลา.
 *
 * ทำไมต้อง portal: `.panel`/`.lg-refract` มี backdrop-filter ซึ่งสร้าง
 * containing block ให้ position:fixed → popover ต้อง portal ไป document.body
 * (แพตเทิร์นเดียวกับ CloseReasonBadge.tsx). วางมุมขวาบน: top = r.bottom + 8,
 * left = r.right - pw (clamp ไม่ให้ล้นขอบ).
 *
 * ไม่มี global store — ใช้ useState + poll ทุก 60 วิ (เหมือน PushNotificationCard).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import Icon, { type IconName } from "./Icon";
import { api } from "@/lib/api";
import type { NotificationItem } from "@/lib/types";

const POLL_MS = 60_000;

/** notification_type → ไอคอน + สี (ตาม UI design system: monotone, ไม่มี emoji). */
const TYPE_META: Record<string, { icon: IconName; tone: string; label: string }> = {
  trade_opened: { icon: "trendUp", tone: "text-profit", label: "เปิดไม้ใหม่" },
  trade_closed: { icon: "checkCircle", tone: "text-slate-300", label: "ปิดไม้แล้ว" },
  stop_loss: { icon: "octagon", tone: "text-loss", label: "ชน Stop Loss" },
  sl_moved: { icon: "shield", tone: "text-amber-300", label: "ขยับ Stop Loss" },
  risk_warning: { icon: "warning", tone: "text-amber-300", label: "เตือนความเสี่ยง" },
  drawdown_warning: { icon: "warning", tone: "text-amber-300", label: "ใกล้ถึงเพดาน Drawdown" },
  limit_expand: { icon: "arrowsH", tone: "text-amber-300", label: "ขอขยายลิมิตความเสี่ยง" },
  economic_news: { icon: "news", tone: "text-slate-300", label: "ข่าวเศรษฐกิจ" },
  daily_digest: { icon: "scroll", tone: "text-slate-300", label: "สรุปตลาดประจำวัน" },
  daily_portfolio_summary: { icon: "scroll", tone: "text-slate-300", label: "สรุปพอร์ตประจำวัน" },
  daily_market_summary: { icon: "scroll", tone: "text-slate-300", label: "สรุปตลาดประจำวัน" },
  weekly_report: { icon: "scroll", tone: "text-slate-300", label: "รายงานรายสัปดาห์" },
  monthly_report: { icon: "scroll", tone: "text-slate-300", label: "รายงานรายเดือน" },
  semi_auto_approval: { icon: "hand", tone: "text-amber-300", label: "รออนุมัติสัญญาณ" },
};

function metaOf(ntype: string) {
  return TYPE_META[ntype] ?? { icon: "bell" as IconName, tone: "text-slate-300", label: ntype };
}

/** ตัด emoji/สัญลักษณ์นำหน้าออกจากข้อความ LINE (UI ห้าม emoji). */
function stripLead(message: string): string {
  return (message || "").replace(/^[^0-9A-Za-z\u0E00-\u0E7F]+/, "").trim();
}

/** "3 นาทีที่แล้ว" — ภาษาไทยแบบสั้น. */
function relTime(iso?: string | null): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return "เมื่อสักครู่";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} นาทีที่แล้ว`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} ชม.ที่แล้ว`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d} วันที่แล้ว`;
  return new Date(iso).toLocaleDateString("th-TH", { day: "numeric", month: "short" });
}

const CHANNEL_LABEL: Record<string, string> = {
  line: "LINE",
  web_push: "Web Push",
  both: "LINE + Push",
  in_app: "ในแอป",
  email: "อีเมล",
};

const STATUS_LABEL: Record<string, string> = {
  sent: "ส่งแล้ว",
  pending: "รอส่ง",
  failed: "ส่งไม่สำเร็จ",
  skipped: "ข้าม",
};

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.notifications(50);
      setItems(res.items ?? []);
      setUnread(res.unread ?? 0);
      setErr(res.verdict !== "ok");
    } catch {
      setErr(true);
    } finally {
      setLoading(false);
    }
  }, []);

  // poll ทุก 60 วิ — เงียบ ๆ ไม่แตะ UI ตอน error (แค่คงค่าเดิมไว้)
  useEffect(() => {
    void load();
    const id = setInterval(() => { void load(); }, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  const place = useCallback(() => {
    const btn = btnRef.current, popEl = popRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const pw = popEl?.offsetWidth ?? 360;
    // มุมขวาบน: ขอบขวาของ popover ตรงกับขอบขวาของปุ่ม
    let left = r.right - pw;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    const top = r.bottom + 8;
    setPos({ top, left });
  }, []);

  useEffect(() => {
    if (!open) return;
    place();
    const close = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node | null;
      if (t && (btnRef.current?.contains(t) || popRef.current?.contains(t))) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("touchstart", close, { passive: true });
    const onScrollOrResize = () => setOpen(false);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("touchstart", close);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, [open, place]);

  const toggle = () => {
    setOpen((v) => {
      const next = !v;
      if (next) {
        setExpanded(null);
        void load(); // รีเฟรชทันทีตอนเปิด
      }
      return next;
    });
  };

  const badge = unread > 99 ? "99+" : String(unread);

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={toggle}
        aria-label="การแจ้งเตือน"
        aria-expanded={open}
        title="การแจ้งเตือน"
        data-testid="notification-bell"
        className="relative inline-flex h-8 w-8 items-center justify-center rounded-full text-slate-300 hover:text-white hover:bg-white/10 transition-colors touch-manipulation"
      >
        <Icon n="bell" size={17} />
        {unread > 0 && (
          <span
            data-testid="notification-badge"
            className="absolute -top-0.5 -right-0.5 min-w-[15px] h-[15px] px-1 rounded-full bg-loss text-[9px] font-bold leading-[15px] text-white text-center"
          >
            {badge}
          </span>
        )}
      </button>

      {open && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label="รายการแจ้งเตือน"
          data-testid="notification-popup"
          style={{
            position: "fixed", top: pos.top, left: pos.left,
            width: "min(380px, calc(100vw - 16px))",
          }}
          className="z-50 animate-pop rounded-xl border border-slate-700 bg-slate-900/95 backdrop-blur shadow-xl overflow-hidden"
        >
          <div className="flex items-center justify-between px-3 py-2 border-b border-slate-700/70">
            <div className="flex items-center gap-1.5 text-xs font-bold text-slate-200">
              <Icon n="bell" size={13} />
              การแจ้งเตือน
              {unread > 0 && (
                <span className="ml-1 rounded-full bg-loss/20 text-loss px-1.5 py-px text-[10px]">
                  ใหม่ {badge}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={() => void load()}
              aria-label="รีเฟรช"
              title="รีเฟรช"
              className="inline-flex h-6 w-6 items-center justify-center rounded-full text-slate-400 hover:text-white hover:bg-white/10 transition-colors"
            >
              <Icon n="refresh" size={13} className={loading ? "animate-spin" : ""} />
            </button>
          </div>

          <div className="max-h-[min(60vh,420px)] overflow-y-auto">
            {err && items.length === 0 && (
              <div className="px-3 py-6 text-center text-xs text-slate-500">
                โหลดการแจ้งเตือนไม่สำเร็จ
              </div>
            )}
            {!err && items.length === 0 && (
              <div className="px-3 py-6 text-center text-xs text-slate-500">
                ยังไม่มีการแจ้งเตือน
              </div>
            )}
            {items.map((it) => {
              const m = metaOf(it.type);
              const isOpen = expanded === it.id;
              const body = stripLead(it.message);
              const firstLine = body.split("\n")[0];
              return (
                <button
                  key={it.id}
                  type="button"
                  onClick={() => setExpanded(isOpen ? null : it.id)}
                  aria-expanded={isOpen}
                  data-testid="notification-row"
                  className="w-full text-left px-3 py-2 border-b border-slate-800/60 last:border-b-0 hover:bg-white/5 transition-colors"
                >
                  <div className="flex items-start gap-2">
                    <span className={`mt-0.5 shrink-0 ${m.tone}`}>
                      <Icon n={m.icon} size={14} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[11px] font-bold text-slate-200 truncate">
                          {m.label}
                        </span>
                        <span className="shrink-0 text-[10px] text-slate-500">
                          {relTime(it.created_at)}
                        </span>
                      </div>
                      <div className={`mt-0.5 text-[11px] text-slate-400 ${isOpen ? "" : "truncate"}`}>
                        {isOpen ? body : firstLine}
                      </div>
                      {isOpen && (
                        <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-slate-500">
                          <span className="rounded bg-white/5 px-1.5 py-px">
                            {CHANNEL_LABEL[it.channel] ?? it.channel}
                          </span>
                          <span className={
                            it.status === "failed" ? "text-loss"
                              : it.status === "sent" ? "text-profit" : "text-slate-400"
                          }>
                            {STATUS_LABEL[it.status] ?? it.status}
                          </span>
                          <span>
                            {new Date(it.created_at).toLocaleString("th-TH")}
                          </span>
                          {it.error && (
                            <span className="text-loss w-full break-words">{it.error}</span>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>,
        document.body
      )}
    </>
  );
}
