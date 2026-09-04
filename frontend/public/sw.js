/* Minimal service worker for the static-export PWA.
 *
 * Strategy:
 * - Navigation requests (HTML): network-first, fall back to cached "/" when
 *   offline so the shell still opens.
 * - Static assets (/_next/static, icons, manifest): cache-first — they are
 *   content-hashed by Next so a new deploy gets new URLs automatically.
 * - API calls (/api/*) and TradingView: NEVER cached — always network, so
 *   prices/signals are never stale.
 *
 * Bumped on deploy: change CACHE_VERSION to invalidate old caches.
 */
const CACHE_VERSION = "v1";
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
