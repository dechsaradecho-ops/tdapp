/**
 * Web Push (VAPID) client helper — Settings page's "การแจ้งเตือนมือถือ" card.
 *
 * WHY: LINE is a third-party app the user must keep installed. Web Push puts
 * the same alerts into the OS notification tray, so a risk warning or SL hit
 * arrives even with the dashboard tab closed.
 *
 * PLATFORM REALITY (this drives the whole UI of the card):
 * - Android (Chrome/Edge/Samsung Internet/Firefox): works from a NORMAL tab,
 *   nothing to install. Chrome enforces `userVisibleOnly` — the service worker
 *   must ALWAYS call showNotification() (see public/sw.js).
 * - iPhone/iPad iOS 16.4+: NO push from a Safari tab. The site must first be
 *   "เพิ่มไปยังหน้าจอโฮม" (installed as PWA) — then the same button works.
 * - In-app browsers (LINE / Facebook / TikTok / Instagram webviews): the Push
 *   API does not exist at all → the card must explain to open in a real
 *   browser, otherwise it looks like a bug.
 * - Desktop browsers: works, but only while the browser is running.
 * - Insecure origin (http://): Push API is unavailable — needs https.
 */

const SW_URL = "/sw.js";

/** True when this browser/runtime can do Web Push at all. */
export function pushSupported(): boolean {
  if (typeof window === "undefined") return false;
  return (
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window &&
    window.isSecureContext
  );
}

/** Current Notification.permission — "denied" cannot be re-asked in-app. */
export function permissionState(): NotificationPermission | "unsupported" {
  if (typeof window === "undefined" || !("Notification" in window)) {
    return "unsupported";
  }
  return Notification.permission;
}

/**
 * True when this is an iOS/iPadOS **Safari tab** (not installed).
 *
 * WHY the separate check: on iOS the button would otherwise silently fail —
 * `pushSupported()` is true in Safari 16.4+ but `pushManager.subscribe()`
 * rejects unless the site runs in standalone (home-screen) mode.
 * iPadOS reports itself as "Macintosh" — the maxTouchPoints check catches it.
 */
export function isIosNeedsInstall(): boolean {
  if (typeof window === "undefined") return false;
  const ua = navigator.userAgent;
  const isIos =
    /iPad|iPhone|iPod/.test(ua) ||
    (ua.includes("Macintosh") && navigator.maxTouchPoints > 1);
  if (!isIos) return false;
  // navigator.standalone = true when launched from the home-screen icon.
  const standalone =
    (navigator as Navigator & { standalone?: boolean }).standalone === true ||
    window.matchMedia("(display-mode: standalone)").matches;
  return !standalone;
}

/** True for embedded webviews (LINE/Facebook/Instagram/TikTok) where Push is absent. */
export function isInAppBrowser(): boolean {
  if (typeof window === "undefined") return false;
  const ua = navigator.userAgent;
  return /Line\/|FBAN|FBAV|Instagram|TikTok|Twitter|MicroMessenger/i.test(ua);
}

/**
 * base64url VAPID public key → Uint8Array for `applicationServerKey`.
 *
 * WHY not atob() alone: atob gives a "binary string"; the key must be the raw
 * bytes. Passing the string (or a padded/base64 non-url variant) makes Chrome
 * throw "InvalidAccessError: The provided applicationServerKey is not valid".
 *
 * NOTE the explicit `Uint8Array<ArrayBuffer>` return type: the DOM lib types
 * `applicationServerKey` as BufferSource and rejects a plain
 * `Uint8Array<ArrayBufferLike>` (a SharedArrayBuffer is not a valid key).
 * Allocating the backing ArrayBuffer keeps the two types identical.
 */
function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = window.atob(normalized);
  const buffer = new ArrayBuffer(raw.length);
  const out = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

async function registration(): Promise<ServiceWorkerRegistration> {
  // getRegistration first: PwaRegister already registers in production, and
  // calling register() again is harmless but this avoids a redundant fetch.
  const existing = await navigator.serviceWorker.getRegistration(SW_URL);
  if (existing) return existing;
  return navigator.serviceWorker.register(SW_URL);
}

/** Serialized subscription exactly as the backend expects it. */
export interface PushPayload {
  endpoint: string;
  keys: { p256dh: string; auth: string };
  user_agent: string;
}

/**
 * Ask permission (if needed) and subscribe this device.
 *
 * Returns the payload to POST to /api/push/subscribe, or throws an Error with
 * a Thai message the card can display verbatim (permission denied, iOS tab,
 * unsupported browser…).
 */
export async function subscribePush(publicKey: string): Promise<PushPayload> {
  if (!pushSupported()) {
    throw new Error("เบราว์เซอร์นี้ไม่รองรับการแจ้งเตือนแบบ Web Push");
  }
  if (isIosNeedsInstall()) {
    throw new Error(
      "บน iPhone/iPad ต้องเพิ่มเว็บนี้ไปยังหน้าจอโฮมก่อน (แชร์ → เพิ่มไปยังหน้าจอโฮม) แล้วเปิดจากไอคอนนั้น"
    );
  }
  if (!publicKey) {
    throw new Error("เซิร์ฟเวอร์ยังไม่ได้ตั้งค่ากุญแจ VAPID");
  }

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error(
      permission === "denied"
        ? "คุณปิดการแจ้งเตือนของเว็บนี้ไว้ — ต้องเปิดคืนในตั้งค่าเบราว์เซอร์ (ไอคอนกุญแจ/ล็อก ข้าง URL)"
        : "ยังไม่ได้กดอนุญาตการแจ้งเตือน"
    );
  }

  const reg = await registration();
  // subscribe() returns the EXISTING subscription when one is valid, so this
  // is also the "re-sync after endpoint rotation" path.
  const existing = await reg.pushManager.getSubscription();
  const sub = existing || (await reg.pushManager.subscribe({
    userVisibleOnly: true,   // REQUIRED — Chrome rejects userVisibleOnly:false
    applicationServerKey: urlBase64ToUint8Array(publicKey),
  }));

  const json = sub.toJSON();
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
    throw new Error("เบราว์เซอร์ไม่ส่งข้อมูลอุปกรณ์ครบถ้วน");
  }
  return {
    endpoint: json.endpoint,
    keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
    user_agent: navigator.userAgent,
  };
}

/** Current endpoint of this device, or null when not subscribed. */
export async function currentEndpoint(): Promise<string | null> {
  if (!pushSupported()) return null;
  try {
    const reg = await navigator.serviceWorker.getRegistration(SW_URL);
    const sub = await reg?.pushManager.getSubscription();
    return sub?.endpoint || null;
  } catch {
    return null;
  }
}

/**
 * Unsubscribe this device in the browser AND return its endpoint so the caller
 * can tell the server. Returns null when there was nothing to remove.
 */
export async function unsubscribePush(): Promise<string | null> {
  if (!pushSupported()) return null;
  try {
    const reg = await navigator.serviceWorker.getRegistration(SW_URL);
    const sub = await reg?.pushManager.getSubscription();
    if (!sub) return null;
    const endpoint = sub.endpoint;
    await sub.unsubscribe();
    return endpoint;
  } catch {
    return null;
  }
}

/**
 * Re-register the current subscription with the server without prompting.
 *
 * Called when the Settings card opens AND on every app start (PwaRegister):
 * if the browser rotated the endpoint (pushsubscriptionchange) or the row was
 * disabled after too many failures, this quietly puts the device back in the
 * list at no cost to the user.
 *
 * Needs no VAPID key — it reuses the subscription the browser already holds.
 * Returns true when a subscription was re-sent.
 */
export async function resyncPush(
  subscribe: (p: PushPayload) => Promise<unknown>
): Promise<boolean> {
  if (permissionState() !== "granted") return false;
  try {
    const reg = await registration();
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return false;
    const json = sub.toJSON();
    if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) return false;
    await subscribe({
      endpoint: json.endpoint,
      keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
      user_agent: navigator.userAgent,
    });
    return true;
  } catch {
    return false;
  }
}

/**
 * App-start self-heal + `pushsubscriptionchange` listener.
 *
 * WHY here and not in the service worker: every /api/ route needs the PIN token
 * in a header, and a service worker cannot read localStorage. The SW therefore
 * only postMessages the page (see public/sw.js) and the page does the POST.
 *
 * Safe to call unconditionally: does nothing unless the user already granted
 * notification permission AND has a live subscription.
 */
export function installPushSync(
  subscribe: (p: PushPayload) => Promise<unknown>
): () => void {
  if (typeof window === "undefined" || !pushSupported()) return () => {};
  void resyncPush(subscribe);

  const onMessage = (event: MessageEvent) => {
    if (event.data?.type === "pushsubscriptionchange") {
      void resyncPush(subscribe);
    }
  };
  navigator.serviceWorker.addEventListener("message", onMessage);
  return () => navigator.serviceWorker.removeEventListener("message", onMessage);
}
