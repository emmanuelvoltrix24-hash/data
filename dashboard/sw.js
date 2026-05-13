const CACHE = 'vfl-v1';
const ASSETS = ['/', '/manifest.json'];

self.addEventListener('install', e => e.waitUntil(
  caches.open(CACHE).then(c => c.addAll(ASSETS))
));

self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/')) return; // never cache API
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request))
  );
});
