// FT-710 Service Worker — basic offline cache
const CACHE = 'ft710-v23';
const ASSETS = [
    '/',
    '/index.html',
    '/ft710.css?v=20',
    '/ft710_main.js?v=23',
    '/ft710_ui.js?v=22',
    '/modules/ptt_manager.js?v=13',
    '/modules/settings_manager.js?v=13',
    '/modules/atr1000.js?v=1',
    '/manifest.json',
];

self.addEventListener('install', function(e) {
    e.waitUntil(
        caches.open(CACHE).then(function(cache) {
            return cache.addAll(ASSETS);
        }).then(function() {
            return self.skipWaiting();
        })
    );
});

self.addEventListener('activate', function(e) {
    e.waitUntil(
        caches.keys().then(function(keys) {
            return Promise.all(keys.map(function(key) {
                if (key !== CACHE) return caches.delete(key);
            }));
        }).then(function() {
            return self.clients.claim();
        })
    );
});

self.addEventListener('fetch', function(e) {
    e.respondWith(
        caches.open(CACHE).then(function(cache) {
            return cache.match(e.request).then(function(resp) {
                return resp || fetch(e.request);
            });
        })
    );
});
