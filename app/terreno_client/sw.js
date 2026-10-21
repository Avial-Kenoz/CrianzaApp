/* Service worker de Terreno (PWA unificada: Dosificación; luego O₂ y Ensilaje).
   Shell network-first con timeout; la API va directo a red. Mismo esquema que
   Sexado (ver su sw.js): con conexión siempre gana el servidor, y si la red no
   contesta en NET_TIMEOUT se sirve el caché para que la app abra igual.

   OJO: al tocar el cliente hay que subir CACHE, APP_VERSION (core.js) y el ?v=
   de index.html juntos. Al sumar un módulo, agregar su .js a ASSETS. */
const CACHE = "terreno-v6";
const NET_TIMEOUT = 2500;
const ASSETS = ["./", "./index.html", "./core.js", "./mod_dosificacion.js",
                "./manifest.webmanifest", "./icon-192.png", "./icon-512.png"];

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
    // ignoreSearch: la página pide core.js?v=N y el precache guarda core.js a secas.
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
