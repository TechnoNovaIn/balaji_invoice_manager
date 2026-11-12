/* Balaji InvoiceHub Service Worker - Strict Zero-Sensitive-Data Cache Policy */
const CACHE_NAME = 'balaji-invoicehub-static-v1';
const OFFLINE_URL = '/offline/';

const PRECACHE_ASSETS = [
  OFFLINE_URL,
  '/static/css/enterprise.css',
  '/static/manifest.json',
  '/static/img/icon-192.png',
  '/static/img/icon-512.png',
];

const NEVER_CACHE_PREFIXES = [
  '/accounts/',
  '/invoices/',
  '/dashboard/',
  '/backup/',
  '/admin/',
  '/media/',
  '/health/',
];

function isSafeStaticRequest(url) {
  return url.origin === self.location.origin && url.pathname.startsWith('/static/');
}

function isSensitiveOrDynamicPath(url) {
  if (url.origin !== self.location.origin) {
    return true;
  }
  return NEVER_CACHE_PREFIXES.some((prefix) => url.pathname.startsWith(prefix));
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'LOGOUT_CLEAR_CACHE') {
    event.waitUntil(
      caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key))))
    );
  }
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') {
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  // Never cache HTML navigations, authenticated pages, PDFs, images under media/invoices, or APIs
  if (request.mode === 'navigate' || request.destination === 'document' || isSensitiveOrDynamicPath(url)) {
    event.respondWith(
      fetch(new Request(request, { cache: 'no-store' })).catch(() => {
        if (request.mode === 'navigate' || request.destination === 'document') {
          return caches.match(OFFLINE_URL);
        }
        return new Response('', { status: 503, statusText: 'Offline' });
      })
    );
    return;
  }

  // Only allow static asset caching for same-origin /static/ resources
  if (isSafeStaticRequest(url)) {
    event.respondWith(
      caches.match(request).then((cached) => {
        const networkFetch = fetch(request)
          .then((resp) => {
            if (resp && resp.ok && resp.type === 'basic') {
              const copy = resp.clone();
              caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
            }
            return resp;
          })
          .catch(() => cached);
        return cached || networkFetch;
      })
    );
  }
});

