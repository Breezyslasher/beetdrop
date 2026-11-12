/* Service worker: NETWORK-FIRST for the app shell, cache as offline
   fallback only. Cache-first served stale app.js against fresh HTML and
   silently broke the UI; network-first can never mix versions. API
   responses and the SSE stream are never cached - job state and search
   results must always be live. */

// Bumped for the Workbench redesign. The name is the version: install
// re-seeds under the new key and activate deletes every other one, so
// the old shell is gone rather than waiting to be overwritten by a
// successful online load. A whole-UI change is exactly when an offline
// launch must not serve the previous look.
const CACHE = "beetdrop-shell-v48";
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
    caches.open(CACHE)
      // cache: "reload" bypasses the browser's own HTTP cache, so a new
      // shell cache is never seeded with the copies it is meant to replace.
      .then((cache) => cache.addAll(
        SHELL.map((url) => new Request(url, { cache: "reload" }))))
      .then(() => self.skipWaiting())
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
  // Only Beetdrop's own files are the app shell. Pictures from elsewhere
  // (YouTube thumbnails, cover art) go straight to the network as the
  // page asked for them, and are not kept in the shell cache.
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/events") return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
