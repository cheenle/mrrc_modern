// MRRC Service Worker — basic offline cache (multi-radio: FT-710 / IC-7300)
const CACHE = 'mrrc-v56';
const ASSETS = [
    '/',
    '/index.html',
    '/ft710.css?v=27',
    '/ft710_main.js?v=46',
    '/ft710_ui.js?v=39',
    '/modules/ptt_manager.js?v=17',
    '/modules/settings_manager.js?v=16',
    '/modules/atr1000.js?v=5',
    '/modules/cloud_hub.js?v=3',
    '/modules/power_switch.js?v=1',
    '/manifest.json',
];

self.addEventListener('install', (e) => {
    e.waitUntil(
        caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys().then((keys) => Promise.all(keys.map((key) => {
                if (key !== CACHE) return caches.delete(key);
            }))).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (e) => {
    e.respondWith(
        caches.open(CACHE).then((cache) => cache.match(e.request).then((resp) => resp || fetch(e.request)))
    );
});
