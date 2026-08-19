/* Service worker de Sexado offline.
   Shell network-first con timeout; la API va directo a red.

   Por qué network-first: con cache-first, un deploy tardaba DOS recargas en
   llegar al tablet (la primera recargaba el sw.js y precacheaba, la segunda
   recién servía el app.js nuevo). Eso dejó equipos corriendo código viejo sin
   forma de notarlo. Ahora, con conexión, siempre gana lo que hay en el servidor.

   Por qué con timeout: un tablet asociado al Wi-Fi pero sin ruta al servidor
   deja el fetch colgado. Si la red no contesta en NET_TIMEOUT, se sirve el
   caché y la app arranca igual; la respuesta de red, si llega, refresca el
   caché para la próxima.

   OJO: al tocar el cliente hay que subir CACHE y APP_VERSION en app.js juntos. */
const CACHE = "sexado-v13";
const NET_TIMEOUT = 2500;
const ASSETS = ["./", "./index.html", "./app.js", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

function networkFirst(req) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (r) => { if (!settled) { settled = true; resolve(r); } };
    // ignoreSearch: la página pide app.js?v=N y el precache guarda app.js a
    // secas. Sin esto, offline el shell no matchea y la app no abre en terreno.
    const fromCache = () => caches.match(req, { ignoreSearch: true });
    const timer = setTimeout(() => {
      fromCache().then((cached) => { if (cached) done(cached); });
    }, NET_TIMEOUT);
    fetch(req).then((res) => {
      clearTimeout(timer);
      if (res && res.status === 200 && res.type === "basic") {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      }
      done(res);
    }).catch(() => {
      clearTimeout(timer);
      fromCache().then((cached) => done(cached || Response.error()));
    });
  });
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.pathname.includes("/api/")) return;   // API: siempre red
  e.respondWith(networkFirst(req));
});
