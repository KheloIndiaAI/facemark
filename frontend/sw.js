/* FaceMark service worker.
 *
 * Its job is to make the app installable and to survive a flaky connection at
 * a sports centre - NOT to cache data. Attendance records, rosters and
 * recognition results are never stored here: they change constantly, they are
 * scoped to whoever is logged in, and a stale roster shown to a coach as
 * current would be worse than no answer at all.
 *
 * So: the shell is cached, /api is never cached.
 */

const VERSION = 'facemark-v6';
const SHELL = [
    '/',
    '/static/css/styles.css',
    '/static/js/icons.js',
    '/static/js/charts.js',
    '/static/js/auth.js',
    '/static/js/pages.js',
    '/static/js/app.js',
    '/icons/icon-192.png',
    '/icons/icon-512.png',
];

self.addEventListener('install', event => {
    event.waitUntil(
        // addAll fails the whole install if any one file 404s, which would
        // leave no worker at all. Each file is added on its own so a renamed
        // asset degrades to "not precached" rather than "no service worker".
        caches.open(VERSION)
            .then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => null))))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', event => {
    const req = event.request;
    if (req.method !== 'GET') return;

    const url = new URL(req.url);
    if (url.origin !== self.location.origin) return;

    // Never cache the API. This includes login state, attendance and every
    // photo of a person - serving any of it from a stale cache would show one
    // coach data that is no longer true, or belongs to someone else.
    if (url.pathname.startsWith('/api/')) return;

    // Navigation: try the network so a deployed change is picked up, and fall
    // back to the cached shell only when genuinely offline.
    if (req.mode === 'navigate') {
        event.respondWith(
            fetch(req).catch(() => caches.match('/', { ignoreSearch: true }))
        );
        return;
    }

    // Code is fetched network-first. Cache-first was serving a stale app.js
    // after an update - the old frontend then runs against the new backend,
    // which is how you get a UI that quietly disagrees with the server. The
    // cache is still there as an offline fallback.
    const isCode = /\.(js|css)$/.test(url.pathname);
    if (isCode) {
        event.respondWith(
            fetch(req).then(res => {
                if (res && res.ok) {
                    const copy = res.clone();
                    caches.open(VERSION).then(c => c.put(req, copy));
                }
                return res;
            }).catch(() => caches.match(req))
        );
        return;
    }

    // Images and fonts do not change without changing their name, so those are
    // served from cache and refreshed quietly behind the scenes.
    event.respondWith(
        caches.match(req).then(hit => {
            const net = fetch(req).then(res => {
                if (res && res.ok) {
                    const copy = res.clone();
                    caches.open(VERSION).then(c => c.put(req, copy));
                }
                return res;
            }).catch(() => hit);
            return hit || net;
        })
    );
});
