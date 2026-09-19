/* Minimal service worker for the static-export PWA.
 *
 * Strategy:
 * - Navigation requests (HTML): network-first, fall back to cached "/" when
 *   offline so the shell still opens.
 * - Static assets (/_next/static, icons, manifest): cache-first — they are
 *   content-hashed by Next so a new deploy gets new URLs automatically.
 * - API calls (/api/*) and TradingView: NEVER cached — always network, so
 *   prices/signals are never stale.
 * - Web Push: `push` shows the alert in the OS notification tray, so a risk
 *   warning / SL hit reaches the phone even with the tab closed.
 *
 * Bumped on deploy: change CACHE_VERSION to invalidate old caches.
 */
const CACHE_VERSION = "v3";
const SHELL_CACHE = `tdapp-shell-${CACHE_VERSION}`;
const ASSET_CACHE = `tdapp-assets-${CACHE_VERSION}`;

// Never intercept these — live data must always hit the network.
const PASS_THROUGH = [
  "/api/",
  "s3.tradingview.com",
  "tradingview.com",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll([
      "/",
      "/manifest.webmanifest",
      "/icons/icon-192.png",
      "/icons/icon-512.png",
    ])).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k.startsWith("tdapp-") && !k.endsWith(CACHE_VERSION))
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (PASS_THROUGH.some((p) => url.href.includes(p))) return; // live data → network

  // HTML navigations: network-first, cached shell as offline fallback.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() =>
          caches.match(req).then((hit) => hit || caches.match("/"))
        )
    );
    return;
  }

  // Static assets: cache-first (Next hashes filenames → safe to cache hard).
  const isStatic =
    url.origin === self.location.origin &&
    (url.pathname.startsWith("/_next/static/") ||
      url.pathname.startsWith("/icons/") ||
      url.pathname === "/manifest.webmanifest");
  if (isStatic) {
    event.respondWith(
      caches.match(req).then((hit) =>
        hit ||
        fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(ASSET_CACHE).then((c) => c.put(req, copy));
          return res;
        })
      )
    );
  }
  // อย่างอื่น (เช่น /_next/image, ไฟล์อื่น) ปล่อยผ่าน network ตามปกติ
});

// ---------------------------------------------------------------------------
// Web Push — แจ้งเตือนเข้า notification tray ของมือถือ/เดสก์ท็อป
// ---------------------------------------------------------------------------
// สำคัญ (Android): Chrome บังคับ userVisibleOnly — ถ้า handler นี้ไม่เรียก
// showNotification() ทุกครั้ง Chrome จะขึ้น notification กลาง ๆ ของตัวเองแทน
// ("This site has been updated in the background") แล้วข้อความจริงหายไป
// ดังนั้น: แสดงผลเสมอ แม้ payload จะว่างหรือ parse ไม่ได้

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    // payload ไม่ใช่ JSON — ใช้ข้อความดิบถ้ามี
    try { data = { body: event.data ? event.data.text() : "" }; } catch (e2) { data = {}; }
  }
  const title = data.title || "AI Trading";
  const body = data.body || "มีการแจ้งเตือนใหม่";
  const url = data.url || "/monitor.html";
  const tag = data.tag || "tdapp";

  event.waitUntil(
    self.registration.showNotification(title, {
      body: body,
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      // tag เดียวกัน = แทนที่อันเก่า ไม่กองทับกันไม่จำกัด
      tag: tag,
      // renotify = สั่น/ดังใหม่แม้ tag เดิม (ต้องมี tag ด้วย)
      renotify: true,
      data: { url: url },
    }).catch(() => {
      // showNotification() โยน TypeError ได้ (เช่น SW ยังไม่ activated หรือ
      // renotify+tag ถูกปฏิเสธ) — ถ้าปล่อยให้ reject Chrome จะขึ้น
      // "This site has been updated in the background" แทนข้อความจริง
      // จึงลองใหม่แบบตัด renotify/tag ออก เพื่อให้ข้อความยังขึ้นเสมอ
      return self.registration.showNotification(title, {
        body: body,
        icon: "/icons/icon-192.png",
        badge: "/icons/icon-192.png",
        data: { url: url },
      });
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/monitor.html";

  // ถ้ามีแท็บของแอปเปิดอยู่แล้ว → focus แท็บนั้น ไม่เปิดใหม่ซ้อน
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          if (client.url.indexOf(self.location.origin) === 0 && "focus" in client) {
            client.navigate(target).catch(() => {});
            return client.focus();
          }
        }
        return self.clients.openWindow(target);
      })
  );
});

// เบราว์เซอร์หมุน endpoint ใหม่เป็นครั้งคราว (เช่น ผู้ใช้ล้างข้อมูลเว็บ)
// → ฝากให้ "หน้าเว็บ" ลงทะเบียนใหม่ ไม่ใช่ fetch เองจากตรงนี้
// เหตุผล: API ทุกเส้นใต้ /api/ ต้องมี PIN token ใน header ซึ่ง service worker
// อ่านไม่ได้ (ไม่มี localStorage) — fetch จากตรงนี้จะได้ 401 เงียบ ๆ
// ถ้าไม่มีแท็บเปิดอยู่เลย หน้า Settings จะ re-sync ให้เองตอนเปิดครั้งถัดไป
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          client.postMessage({ type: "pushsubscriptionchange" });
        }
      })
  );
});
