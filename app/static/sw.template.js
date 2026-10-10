/* Trading Journal service worker (version __VERSION__; served at /sw.js, scope "/").
 *
 * Deliberately tiny and safe:
 *  - caches ONLY same-origin GETs under /static/ (icons, CSS, JS) and the static /offline page;
 *  - network-first for /static/ (so edits roll out immediately), cache only as the offline fallback;
 *  - page navigations always go to the network; if the network fails the cached /offline page is shown;
 *  - never touches non-GET requests, /api/*, htmx/XHR calls, login, cookies or cross-origin (CDN) requests,
 *    and never stores an authenticated HTML page or JSON response.
 * The version string is a hash of the static files, so a changed asset ships a new worker and the old
 * cache is deleted on activate.
 */
const VERSION = '__VERSION__';
const CACHE = 'tj-static-' + VERSION;
const PRECACHE = ['/offline', '/static/icons/icon-192.png', '/static/icons/favicon-32.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => Promise.all(PRECACHE.map((u) => fetch(u, { credentials: 'omit', cache: 'reload' })
        .then((r) => (r.ok ? c.put(u, r) : null)).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('tj-static-') && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;                       // POSTs (login, journal, ingest...) pass straight through
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;        // CDNs etc.: browser default

  if (req.mode === 'navigate') {                          // HTML pages: network only, friendly page when offline
    event.respondWith(fetch(req).catch(() => caches.match('/offline').then((r) => r || new Response('Offline', { status: 503 }))));
    return;
  }
  if (url.pathname.startsWith('/static/')) {              // public assets: network first, cache as fallback
    event.respondWith(
      fetch(req).then((res) => {
        if (res.ok && res.type === 'basic') { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
        return res;
      }).catch(() => caches.match(req).then((r) => r || Response.error()))
    );
  }
  // anything else (API, htmx fragments, /sync polls, ingest...): not handled -> normal network behaviour
});
