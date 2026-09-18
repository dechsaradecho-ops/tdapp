"use client";

/**
 * PushNotificationCard — การ์ด "การแจ้งเตือนมือถือ (Web Push)" ในหน้า Settings
 *
 * ทำไมต้องมี: LINE เป็นแอปของบุคคลที่สาม ผู้ใช้ที่ไม่อยากติดตั้ง LINE จะไม่
 * ได้รับแจ้งเตือนเลย — Web Push ยิงเข้าถึง notification tray ของ OS ตรง ๆ
 * แม้ปิดแท็บไว้ (ยังคงส่ง LINE คู่กันไป ไม่ได้แทนกัน)
 *
 * เนื้อหาที่การ์ดต้องบอกให้ครบ (ไม่งั้นผู้ใช้จะคิดว่าเป็นบั๊ก):
 *  1. เบราว์เซอร์นี้รองรับไหม / ยังไม่เปิดสิทธิ์ / ถูกปฏิเสธไปแล้ว
 *  2. iPhone/iPad — ต้อง "เพิ่มไปยังหน้าจอโฮม" ก่อน (Safari แท็บธรรมดาไม่รับ push)
 *  3. อุปกรณ์ที่ผูกไว้มีกี่เครื่อง (endpoint ไม่ถูกส่งออกมาจากเซิร์ฟเวอร์)
 *  4. ปุ่มทดสอบ — กดแล้วยิงจริงไปทุกเครื่อง และบอกผลรายอุปกรณ์
 *  5. เมื่อไรที่ปุ่มถูกปิด เพราะอะไร (เซิร์ฟเวอร์ไม่มี VAPID key / ไม่รองรับ)
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Icon from "@/components/Icon";
import { api } from "@/lib/api";
import {
  currentEndpoint,
  isInAppBrowser,
  isIosNeedsInstall,
  permissionState,
  pushSupported,
  resyncPush,
  subscribePush,
  unsubscribePush,
} from "@/lib/push";
import type { PushKeyInfo, PushSubscriptionsResponse, PushTestResult } from "@/lib/types";

export default function PushNotificationCard() {
  const [info, setInfo] = useState<PushKeyInfo | null>(null);
  const [subs, setSubs] = useState<PushSubscriptionsResponse | null>(null);
  const [myEndpoint, setMyEndpoint] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [msgOk, setMsgOk] = useState<boolean | null>(null);
  const [testRes, setTestRes] = useState<PushTestResult | null>(null);
  // Distinguishes "the API answered: VAPID is not set" from "the API did not
  // answer at all" — otherwise a down backend paints a red X next to a setting
  // the owner has not actually forgotten to configure.
  const [loadErr, setLoadErr] = useState(false);

  // Environment facts are stable for the page's lifetime — compute once.
  const supported = useMemo(() => pushSupported(), []);
  const iosNeedsInstall = useMemo(() => isIosNeedsInstall(), []);
  const inApp = useMemo(() => isInAppBrowser(), []);
  const [perm, setPerm] = useState<NotificationPermission | "unsupported">("unsupported");

  const load = useCallback(async () => {
    setPerm(permissionState());
    let keyInfo: PushKeyInfo | null = null;
    try {
      keyInfo = await api.pushKey();
      setLoadErr(false);
    } catch {
      setLoadErr(true);
    }
    const [s, ep] = await Promise.all([
      api.pushSubscriptions().catch(() => null),
      currentEndpoint(),
    ]);
    setInfo(keyInfo);
    setSubs(s);
    setMyEndpoint(ep);
    // Self-heal: ถ้าเบราว์เซอร์หมุน endpoint ใหม่ หรือแถวฝั่งเซิร์ฟเวอร์ถูกปิด
    // (fail_count เกินเพดาน) การเปิดหน้านี้จะลงทะเบียนใหม่ให้เงียบ ๆ
    if (keyInfo?.enabled && ep && perm === "granted") {
      const resent = await resyncPush(api.pushSubscribe);
      if (resent) api.pushSubscriptions().then(setSubs).catch(() => {});
    }
  }, [perm]);

  useEffect(() => { void load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const subscribe = async () => {
    setBusy(true); setMsg(""); setMsgOk(null); setTestRes(null);
    try {
      if (!info?.enabled) {
        setMsgOk(false);
        setMsg("เซิร์ฟเวอร์ยังไม่มีกุญแจ VAPID — ต้องตั้ง env ก่อน (ดูวิธีในข้อความด้านบน)");
        return;
      }
      const payload = await subscribePush(info.public_key);
      const r = await api.pushSubscribe(payload);
      setMsgOk(r.ok);
      setMsg(r.message || (r.ok ? "ผูกอุปกรณ์นี้แล้ว" : "ลงทะเบียนไม่สำเร็จ"));
      await load();
    } catch (e) {
      setMsgOk(false);
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const unsubscribe = async () => {
    setBusy(true); setMsg(""); setMsgOk(null); setTestRes(null);
    try {
      const endpoint = await unsubscribePush();
      if (!endpoint) {
        setMsgOk(true);
        setMsg("อุปกรณ์นี้ยังไม่ได้ผูกอยู่");
      } else {
        const r = await api.pushUnsubscribe(endpoint);
        setMsgOk(r.ok);
        setMsg(r.message || (r.ok ? "ปิดการแจ้งเตือนบนอุปกรณ์นี้แล้ว" : "ปิดไม่สำเร็จ"));
      }
      await load();
    } catch (e) {
      setMsgOk(false);
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runTest = async () => {
    setBusy(true); setMsg(""); setMsgOk(null); setTestRes(null);
    try {
      const r = await api.pushTest();
      setTestRes(r);
      setMsgOk(r.ok);
      setMsg(r.message);
    } catch (e) {
      setMsgOk(false);
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // เหตุผลที่ปุ่ม "เปิดการแจ้งเตือน" กดไม่ได้ — ต้องบอกให้ชัด ไม่ใช่ disable เงียบ ๆ
  const blockedReason = (() => {
    if (inApp) return "เบราว์เซอร์ในแอป (LINE/Facebook ฯลฯ) ไม่รองรับการแจ้งเตือน — เปิดลิงก์นี้ใน Chrome หรือ Safari แทน";
    if (!supported) return "เบราว์เซอร์นี้ไม่รองรับ Web Push — ใช้ Chrome, Edge, Safari (iOS 16.4+) หรือ Firefox";
    if (iosNeedsInstall) return "บน iPhone/iPad ต้องกด แชร์ → เพิ่มไปยังหน้าจอโฮม ก่อน แล้วเปิดจากไอคอนนั้นจึงจะเปิดการแจ้งเตือนได้";
    if (perm === "denied") return "คุณปิดการแจ้งเตือนของเว็บนี้ไว้ — ต้องเปิดคืนในตั้งค่าเบราว์เซอร์ (ไอคอนกุญแจ/ล็อก ข้าง URL) แล้วโหลดหน้าใหม่";
    return "";
  })();

  const thisDeviceOn = Boolean(myEndpoint);
  const othersOn = Math.max(0, (subs?.enabled_count || 0) - (thisDeviceOn ? 1 : 0));

  return (
    <div className="panel">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="panel-title flex items-center gap-2">
          <Icon n="bell" size={16} /> การแจ้งเตือนมือถือ
        </h2>
        <div className="flex items-center gap-2">
          <button
            onClick={runTest}
            disabled={busy || !supported}
            aria-busy={busy}
            className="bg-accent text-white font-semibold rounded px-4 py-2.5 min-h-[44px] disabled:opacity-50 active:brightness-90"
          >
            <span className="inline-flex items-center gap-1.5">
              {busy && <Icon n="spinner" size={14} className="animate-spin" />}
              {busy ? "กำลังส่ง..." : "ทดสอบการแจ้งเตือน"}
            </span>
          </button>
          {thisDeviceOn ? (
            <button
              onClick={unsubscribe}
              disabled={busy}
              className="text-xs text-slate-400 border border-slate-700 rounded px-3 min-h-[44px] active:bg-slate-800 disabled:opacity-40"
            >
              ปิดบนเครื่องนี้
            </button>
          ) : (
            <button
              onClick={subscribe}
              disabled={busy || !supported || Boolean(blockedReason)}
              className="text-xs text-slate-300 border border-slate-600 rounded px-3 min-h-[44px] active:bg-slate-800 disabled:opacity-40"
            >
              เปิดการแจ้งเตือนบนอุปกรณ์นี้
            </button>
          )}
        </div>
      </div>

      <p className="text-sm text-slate-400 mt-1">
        แจ้งเตือนเข้ามือถือ/เดสก์ท็อปผ่านเบราว์เซอร์ — ได้แม้ปิดแท็บไว้
        และยังคงส่ง LINE ตามเดิม (ทำงานคู่กัน ไม่ได้แทนกัน)
      </p>

      {/* --- สถานะ --- */}
      <div className="mt-4 rounded border border-slate-700 bg-surface/40 p-3 space-y-1.5">
        <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-1 flex items-center gap-1.5">
          <Icon n="bolt" size={13} /> สถานะอุปกรณ์นี้
        </p>
        <StatusRow
          label="เบราว์เซอร์รองรับ Web Push"
          ok={supported}
          note={supported ? undefined : "ต้องเป็น https และมี Service Worker + Push API"}
        />
        <StatusRow
          label="อนุญาตการแจ้งเตือนแล้ว"
          ok={perm === "granted"}
          note={
            perm === "granted" ? undefined
              : perm === "denied" ? "ถูกปิดไว้ — เปิดคืนได้จากไอคอนข้าง URL"
              : perm === "unsupported" ? "เบราว์เซอร์นี้ไม่รู้จัก Notification API"
              : "ยังไม่ได้ขอสิทธิ์ — กดปุ่มด้านขวาได้เลย"
          }
        />
        <StatusRow label="อุปกรณ์นี้ผูกกับเซิร์ฟเวอร์แล้ว" ok={thisDeviceOn} />
        <StatusRow
          label="เซิร์ฟเวอร์พร้อมส่ง (VAPID key)"
          ok={Boolean(info?.enabled)}
          note={
            info?.enabled
              ? undefined
              : loadErr
                ? "ติดต่อเซิร์ฟเวอร์ไม่ได้ — รีเฟรชหน้าเพื่อลองใหม่"
                : "ยังไม่ได้ตั้ง VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY"
          }
        />
        <p className="text-xs text-slate-500 pt-1">
          อุปกรณ์ที่เปิดอยู่ทั้งหมด: {subs?.enabled_count ?? 0} เครื่อง
          {othersOn > 0 && ` (เครื่องอื่น ${othersOn})`}
          {subs && subs.total > subs.enabled_count &&
            ` · ปิด/หมดอายุแล้ว ${subs.total - subs.enabled_count}`}
        </p>
      </div>

      {/* --- คำอธิบายเมื่อใช้งานไม่ได้ --- */}
      {blockedReason && (
        <p className="text-amber-400 text-xs mt-3 flex items-start gap-1.5">
          <Icon n="warning" size={13} className="mt-0.5 shrink-0" />
          <span>{blockedReason}</span>
        </p>
      )}

      {loadErr && (
        <p className="text-amber-400 text-xs mt-2 flex items-start gap-1.5">
          <Icon n="warning" size={13} className="mt-0.5 shrink-0" />
          <span>โหลดสถานะการแจ้งเตือนไม่สำเร็จ (API อาจกำลังเริ่มต้น) — รีเฟรชหน้าเพื่อลองอีกครั้ง</span>
        </p>
      )}

      {info && !info.enabled && info.message && (
        <p className="text-amber-400 text-xs mt-2 flex items-start gap-1.5">
          <Icon n="bulb" size={13} className="mt-0.5 shrink-0" />
          <span>{info.message}</span>
        </p>
      )}

      {msg && (
        <p className={`text-sm mt-2 ${msgOk === false ? "text-loss" : msgOk ? "text-profit" : ""}`}>
          {msg}
        </p>
      )}

      {/* --- ผลทดสอบรายอุปกรณ์ --- */}
      {testRes && testRes.results.length > 0 && (
        <div className="mt-3 space-y-1">
          <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">
            ผลทดสอบรายอุปกรณ์ (ส่งได้ {testRes.sent} / ล้มเหลว {testRes.failed})
          </p>
          {testRes.results.map((r, i) => (
            <div key={`${r.device}-${i}`} className="text-xs">
              <div className="flex items-center gap-2">
                <span className={r.ok ? "text-profit" : "text-loss"}>
                  <Icon n={r.ok ? "checkCircle" : "xCircle"} size={13} />
                </span>
                <span className="text-slate-300">{r.device || "ไม่ทราบอุปกรณ์"}</span>
                {r.ok && <span className="text-slate-500">— ควรเห็นใน notification tray</span>}
              </div>
              {r.error && <p className="text-loss ml-6 break-all">{r.error}</p>}
            </div>
          ))}
        </div>
      )}

      {/* --- รายชื่ออุปกรณ์ (ไม่มี endpoint/keys — เป็น credential) --- */}
      {subs && subs.devices.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-1">
            อุปกรณ์ที่ลงทะเบียนไว้
          </p>
          <div className="space-y-1">
            {subs.devices.map((d) => (
              <div key={String(d.id)} className="flex items-center gap-2 text-xs">
                <span className={d.enabled ? "text-profit" : "text-slate-600"}>
                  <Icon n={d.enabled ? "checkCircle" : "ban"} size={13} />
                </span>
                <span className="text-slate-300">{d.device || "ไม่ทราบอุปกรณ์"}</span>
                {d.last_error && (
                  <span className="text-slate-500 break-all">— {d.last_error}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* --- วิธีเปิดใช้บนมือถือ --- */}
      <div className="mt-3 rounded border border-slate-700 bg-surface/40 p-3">
        <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-1 flex items-center gap-1.5">
          <Icon n="bulb" size={13} /> วิธีเปิดใช้บนมือถือ
        </p>
        <ol className="text-xs text-slate-400 space-y-1 list-decimal ml-4">
          <li>Android: เปิดเว็บนี้ใน Chrome → กด &quot;เปิดการแจ้งเตือนบนอุปกรณ์นี้&quot; → อนุญาต (ไม่ต้องติดตั้งแอป)</li>
          <li>iPhone/iPad (iOS 16.4+): กด แชร์ → เพิ่มไปยังหน้าจอโฮม → เปิดจากไอคอนบนหน้าจอโฮม → กดปุ่มเดียวกัน</li>
          <li>ถ้าไม่ได้รับ: เช็คว่าไม่ได้ปิดสิทธิ์การแจ้งเตือนของเบราว์เซอร์ใน Settings ของเครื่อง และโหมดห้ามรบกวน (Do Not Disturb) ปิดอยู่</li>
          <li>กด &quot;ทดสอบการแจ้งเตือน&quot; เพื่อยืนยันว่าปลายทางยังทำงาน</li>
        </ol>
      </div>
    </div>
  );
}

function StatusRow({ label, ok, note }: { label: string; ok: boolean; note?: string }) {
  return (
    <div className="flex items-start gap-2 text-xs">
      <span className={ok ? "text-profit" : "text-loss"}>
        <Icon n={ok ? "checkCircle" : "xCircle"} size={13} />
      </span>
      <span className="text-slate-300">{label}</span>
      {note && <span className="text-slate-500">— {note}</span>}
    </div>
  );
}
