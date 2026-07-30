/* Minimal service worker: caches the app shell only, so the PWA opens
   instantly from a home screen. API responses and the SSE stream are
   never cached - job state and search results must always be live. */

const CACHE = "trackpull-shell-v1";
const SHELL = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/static/vue.global.prod.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/events") return;
  event.respondWith(
    caches.match(event.request).then(
      (cached) => cached || fetch(event.request)
    )
  );
});
