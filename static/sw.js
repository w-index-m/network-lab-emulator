// ネットワークラボ エミュレーター Service Worker
// UIシェル（HTML/manifest/アイコン）をキャッシュ。API/WebSocketは常にネットワーク。
const CACHE = 'netlab-shell-v1';
const SHELL = [
  '/',
  '/static/manifest.webmanifest',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // API/WebSocket/認証は常にネットワーク（キャッシュしない）
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) {
    return; // デフォルトのネットワーク処理に任せる
  }
  // 静的シェルは network-first（更新を優先しつつオフライン時はキャッシュ）
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res && res.status === 200 && e.request.method === 'GET') {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match('/')))
  );
});
