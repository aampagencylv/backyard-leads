// Prospector service worker — v1.
//
// What it caches:
//   - The app shell (/, /static/index.html) for offline boot
//   - Static icons + the manifest
//
// What it deliberately does NOT cache:
//   - /api/* — those are auth'd + freshness-sensitive; always network
//   - /static/index.html in stale-mode for too long — when we ship a
//     hot fix, users get it on next reload
//
// Strategy:
//   - Cache First for static assets (icons, fonts) — they rarely change
//     and we want them instant on second load
//   - Stale-While-Revalidate for the shell HTML — serve from cache for
//     speed, refresh in background so the next visit has the new bytes
//   - Network Only for anything under /api/ — never cache API responses
//
// To bust the cache after a deploy, bump SW_VERSION below. The browser
// activates the new worker on the next page load (or immediately if
// `skipWaiting()` is called — which we do here).

const SW_VERSION = "v2.17.0";
const SHELL_CACHE = `prospector-shell-${SW_VERSION}`;
const STATIC_CACHE = `prospector-static-${SW_VERSION}`;

const SHELL_URLS = [
  "/",
  "/manifest.webmanifest",
  "/static/pwa/icon-192.png",
  "/static/pwa/icon-512.png",
  "/static/pwa/icon-512-maskable.png",
  "/static/pwa/apple-touch-icon-180.png",
];

// ----------------------------------------------------------------------
// Install — pre-cache the shell
// ----------------------------------------------------------------------

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS)).then(() => self.skipWaiting())
  );
});

// ----------------------------------------------------------------------
// Activate — purge old versions
// ----------------------------------------------------------------------

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys
        .filter((k) => k.startsWith("prospector-") && !k.endsWith(SW_VERSION))
        .map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

// ----------------------------------------------------------------------
// Fetch — routing
// ----------------------------------------------------------------------

self.addEventListener("fetch", (event) => {
  const req = event.request;
  // Only handle GETs — never cache POST/PATCH/DELETE
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Same-origin only — Twilio SDK / external scripts go straight to net
  if (url.origin !== self.location.origin) return;

  // API: always network, never cache
  if (url.pathname.startsWith("/api/")) return;

  // Integrations iframes: always network (auth'd, fresh)
  if (url.pathname.startsWith("/integrations/")) return;

  // Static assets: cache-first
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(req, STATIC_CACHE));
    return;
  }

  // Shell (root + HTML): network-first so code changes take effect immediately.
  // Falls back to cache only when offline.
  event.respondWith(networkFirst(req, SHELL_CACHE));
});

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  if (cached) return cached;
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (e) {
    return cached || Response.error();
  }
}

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (e) {
    const cached = await cache.match(req);
    return cached || Response.error();
  }
}
