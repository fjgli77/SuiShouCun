/* PWA：只缓存同一套界面静态资源。API 与下载请求一律走网络。
   v6 起 PC 与移动端是同一个页面 /，不再有 /m 与 /pc 两套资源。
   下面的缓存名占位符由服务端返回本文件时替换为真实版本号，
   保证改版后缓存名随之变化、用户不会一直拿到旧 app.js。 */
const CACHE = "media-dl-__VERSION__";
const ASSETS = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/static/icon.svg",
  "/manifest.webmanifest"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  event.respondWith(
    fetch(req).then((resp) => {
      if (resp && resp.status === 200 && resp.type === "basic") {
        const copy = resp.clone();
        caches.open(CACHE).then((cache) => cache.put(req, copy)).catch(() => {});
      }
      return resp;
    }).catch(() => caches.match(req))
  );
});
