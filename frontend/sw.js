// Aegis Quant service worker: caches the app shell so the PWA opens
// instantly (and installs), never caches API responses - they carry the
// auth token and live trading data that must never be served stale.
const CACHE = "aegis-shell-v2";
const SHELL = [
  "/", "/static/dashboard.css", "/static/dashboard.js", "/manifest.webmanifest",
  "/static/icons/icon-192.png", "/static/icons/icon-512.png", "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")) return; // always network, never cached

  const sameOrigin = url.origin === self.location.origin;
  const isCdn = url.hostname === "cdn.jsdelivr.net" || url.hostname === "fonts.googleapis.com"
    || url.hostname === "fonts.gstatic.com";
  if (!sameOrigin && !isCdn) return;

  // Network first (so a new deploy shows up right away), cache as offline fallback.
  event.respondWith(
    fetch(req).then((resp) => {
      if (resp.ok || resp.type === "opaque") {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      }
      return resp;
    }).catch(() => caches.match(req).then((hit) => hit || (req.mode === "navigate" ? caches.match("/") : undefined))),
  );
});
