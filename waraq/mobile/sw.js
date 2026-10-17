/* Public UI only. Bump VERSION whenever a precached asset changes. */
'use strict';
const VERSION = '2026-10-17-chatgpt-accounts-1';
const CACHE = 'waraq-public-' + VERSION;
const PUBLIC_ASSETS = [
  '/pwa-shell.html', '/offline.html', '/manifest.webmanifest',
  '/assets/app.js', '/assets/style.css', '/assets/pwa.js', '/assets/icon.svg',
  '/assets/vendor/jsQR.js',
  '/assets/icons/icon-192.png', '/assets/icons/icon-512.png',
  '/assets/icons/maskable-192.png', '/assets/icons/maskable-512.png',
  '/assets/icons/apple-touch-icon.png'
];
const allowed = new Set(PUBLIC_ASSETS);
const pageRoute = path => path === '/internet' || path === '/pair' || /^\/devices\/[A-Za-z0-9_-]{1,128}$/.test(path);

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    try {
      // addAll is atomic: a failed asset fetch leaves the previous active
      // worker/cache usable. Cookies are never sent for public precaching.
      await cache.addAll(PUBLIC_ASSETS.map(path => new Request(path, {credentials:'omit', cache:'reload'})));
    } catch (error) {
      await caches.delete(CACHE);
      throw error;
    }
    // Updates wait for old tabs to close or the explicit coordinated action.
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith('waraq-public-') && name !== CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  // No runtime cache writes. API, downloads, previews and authenticated
  // device URLs are never cache keys, regardless of method or query string.
  if (!url.search && allowed.has(url.pathname)) {
    event.respondWith((async () => (await caches.open(CACHE)).match(url.pathname).then(hit => hit || fetch(request)))());
    return;
  }
  if (request.mode === 'navigate' && !url.search && pageRoute(url.pathname)) {
    event.respondWith((async () => {
      try { return await fetch(request); } // HTTP 401/403/500 stay HTTP errors.
      catch (_) {
        const cache = await caches.open(CACHE);
        return (await cache.match('/pwa-shell.html')) || (await cache.match('/offline.html'));
      }
    })());
  }
});

// A waiting worker polls every same-origin tab. Missing/hidden/unresponsive
// tabs fail closed; a second pass just before activation closes the busy race.
let activating = false;
async function allTabsIdle() {
  const windows = await self.clients.matchAll({type:'window', includeUncontrolled:true});
  const results = await Promise.all(windows.map(client => new Promise(resolve => {
    const channel = new MessageChannel();
    const timer = setTimeout(() => { channel.port1.close(); resolve(false); }, 2000);
    channel.port1.onmessage = event => {
      clearTimeout(timer); channel.port1.close(); resolve(event.data?.idle === true);
    };
    client.postMessage({type:'WARAQ_UPDATE_CHECK'}, [channel.port2]);
  })));
  return results.every(Boolean);
}
self.addEventListener('message', event => {
  if (event.data?.type !== 'WARAQ_APPLY_UPDATE' || !event.source || activating) return;
  event.waitUntil((async () => {
    activating = true;
    if (await allTabsIdle() && await allTabsIdle()) {
      await self.skipWaiting();
    } else {
      for (const client of await self.clients.matchAll({type:'window', includeUncontrolled:true})) {
        client.postMessage({type:'WARAQ_UPDATE_UNLOCK'});
      }
      event.source.postMessage({type:'WARAQ_UPDATE_BLOCKED'});
    }
    activating = false;
  })());
});
