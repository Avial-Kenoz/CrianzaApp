"use strict";
/* Terreno — núcleo común de la PWA unificada (Dosificación; luego O₂ y Ensilaje).

   Lo que es de todos vive aquí: almacenamiento local, UNA cola de envío,
   pestañas y la sincronización. No se elige operador: quien registra en la
   tablet es «Turno» (cambia todo el tiempo; decisión del usuario 19-10). Cada módulo se registra
   con Terreno.register({...}) y solo sabe de su dominio.

   Aislamiento: init/render/flush/refresh de cada módulo corren en try/catch.
   Si un módulo revienta, su pestaña muestra el error y los demás siguen
   (con apps separadas esto venía gratis; al juntarlas hay que cuidarlo).

   Sincronización: al abrir, al guardar, al volver la red y cada minuto con la
   app visible. Se suben las colas de TODOS los módulos, esté abierta la
   pestaña que esté. Ninguna app sincroniza cerrada (no hay Background Sync).

   OJO al desplegar: subir APP_VERSION, CACHE en sw.js y ?v= en index.html. */

const APP_VERSION = "v6";
const TICK_MS = 60000;

// localStorage puede estar bloqueado (tablet Getac, jul-2026): fallback a memoria.
const _mem = {};
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return (k in _mem) ? _mem[k] : null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { _mem[k] = v; } }

const $ = (id) => document.getElementById(id);
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function uuid() {
  return crypto.randomUUID ? crypto.randomUUID()
    : String(Date.now()) + Math.random().toString(16).slice(2);
}
// Números a la chilena: 8,3 · 25 · 1.250
function fmtKg(v) {
  if (v == null || !isFinite(v)) return "—";
  const r = Math.round(v * 10) / 10;
  return r.toLocaleString("es-CL", { maximumFractionDigits: 1 });
}
function parseNum(s) {
  if (s == null) return NaN;
  const t = String(s).trim().replace(",", ".");
  return t === "" ? NaN : Number(t);
}
function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}/${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function localISO(d) {   // fecha-hora local sin zona, como la guarda el servidor
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function todayStr() { return localISO(new Date()).slice(0, 10); }

// fetch con timeout: un tablet asociado al Wi-Fi pero sin ruta deja el fetch
// colgado indefinidamente. Los errores de red salen marcados con .net = true.
async function fetchJSON(url, opts, ms) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms || 8000);
  let res;
  try { res = await fetch(url, Object.assign({ signal: ctl.signal, cache: "no-store" }, opts || {})); }
  catch (e) { const err = new Error("Sin conexión con la base"); err.net = true; throw err; }
  finally { clearTimeout(t); }
  if (!res.ok) throw new Error(`El servidor respondió ${res.status}`);
  return res.json();
}
function isNetErr(e) { return !!(e && e.net); }

let _tt = null;
function toast(m, kind) {
  const t = $("toast"); t.textContent = m; t.className = "toast show " + (kind || "");
  clearTimeout(_tt); _tt = setTimeout(() => t.classList.remove("show"), 2800);
}

// ---------------------------------------------------------------------------
// IndexedDB best-effort: estas funciones NUNCA rechazan (ver Captura O₂).
// ---------------------------------------------------------------------------
let _db = null;
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("terreno", 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta");
      if (!db.objectStoreNames.contains("queue")) db.createObjectStore("queue", { keyPath: "client_uuid" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
function _store(name, mode) { return _db.transaction(name, mode).objectStore(name); }
function idbGet(store, key) {
  if (!_db) return Promise.resolve(undefined);
  return new Promise((res) => {
    try { const r = _store(store, "readonly").get(key); r.onsuccess = () => res(r.result); r.onerror = () => res(undefined); }
    catch (e) { res(undefined); }
  });
}
function idbPut(store, val, key) {
  if (!_db) return Promise.resolve(false);
  return new Promise((res) => {
    try { const r = _store(store, "readwrite").put(val, key); r.onsuccess = () => res(true); r.onerror = () => res(false); }
    catch (e) { res(false); }
  });
}
function idbDel(store, key) {
  if (!_db) return Promise.resolve();
  return new Promise((res) => {
    try { const r = _store(store, "readwrite").delete(key); r.onsuccess = () => res(); r.onerror = () => res(); }
    catch (e) { res(); }
  });
}
function idbAll(store) {
  if (!_db) return Promise.resolve([]);
  return new Promise((res) => {
    try { const r = _store(store, "readonly").getAll(); r.onsuccess = () => res(r.result || []); r.onerror = () => res([]); }
    catch (e) { res([]); }
  });
}

// ---------------------------------------------------------------------------
// Núcleo
// ---------------------------------------------------------------------------
const Terreno = (() => {
  const modules = [];          // en orden de registro = orden de pestañas
  const byId = {};
  // Cola única: {client_uuid, module, payload, created_at, error?}. Se guarda
  // en memoria ANTES de persistir: si el equipo no persiste, igual se sube.
  let queue = [];
  let active = null;
  let syncing = false;
  let canPersist = false;

  function register(mod) { modules.push(mod); byId[mod.id] = mod; }

  function safe(mod, what, fn) {
    try {
      const r = fn();
      if (r && typeof r.catch === "function") return r.catch((e) => fail(mod, what, e));
      return r;
    } catch (e) { fail(mod, what, e); }
  }
  function fail(mod, what, e) {
    if (what === "refresh" && isNetErr(e)) return;   // sin red: se muestra lo descargado
    const sec = $("mod-" + mod.id);
    const msg = (e && e.message) || String(e);
    if (sec && what !== "flush" && what !== "refresh") {
      sec.innerHTML = `<div class="mod-error"><b>${esc(mod.label)} no pudo cargar</b> (${esc(what)}).<br>` +
        `${esc(msg)}<br><br>Los otros módulos siguen funcionando. Avisa a soporte con este mensaje.</div>`;
    } else {
      toast(`${mod.label}: ${msg}`, "err");
    }
  }

  // --- cola ----------------------------------------------------------------
  async function enqueue(moduleId, payload) {
    const item = { client_uuid: payload.client_uuid || uuid(), module: moduleId,
                   payload, created_at: localISO(new Date()) };
    item.payload.client_uuid = item.client_uuid;
    queue.push(item);
    await idbPut("queue", item);
    renderBadges();
    sync();
    return item;
  }
  function pendingFor(moduleId) { return queue.filter((q) => q.module === moduleId); }

  async function sync() {
    if (syncing) return;
    if (!queue.length) { renderBadges(); return; }
    syncing = true;
    try {
      for (const mod of modules) {
        const items = pendingFor(mod.id);
        if (!items.length || typeof mod.flush !== "function") continue;
        // flush devuelve {done:[uuid], errors:{uuid: msg}}; lanza si no hay red.
        // Sin red es lo normal en terreno: se reintenta callado; otro error se avisa.
        let res;
        try { res = await mod.flush(items.map((i) => i.payload)); }
        catch (e) { if (!isNetErr(e)) fail(mod, "flush", e); continue; }
        if (!res) continue;
        const done = new Set(res.done || []);
        for (const u of done) await idbDel("queue", u);
        queue = queue.filter((q) => !done.has(q.client_uuid));
        for (const q of queue) {
          if (res.errors && res.errors[q.client_uuid]) {
            q.error = res.errors[q.client_uuid];
            await idbPut("queue", q);
          }
        }
        if (done.size) safe(mod, "refresh", () => mod.refresh && mod.refresh({ afterSync: true }));
      }
    } finally {
      syncing = false;
      renderBadges();
    }
  }

  // --- confirmación genérica (devuelve promesa) -------------------------------
  function confirmBox(title, text, yes) {
    return new Promise((resolve) => {
      $("cf-title").textContent = title;
      $("cf-text").textContent = text;
      $("cf-yes").textContent = yes || "Sí, registrar";
      const m = $("modal-confirm");
      const close = (v) => { m.classList.remove("show"); $("cf-yes").onclick = null; $("cf-no").onclick = null; resolve(v); };
      $("cf-yes").onclick = () => close(true);
      $("cf-no").onclick = () => close(false);
      m.classList.add("show");
    });
  }

  // --- vista ---------------------------------------------------------------
  function renderBadges() {
    const errs = queue.filter((q) => q.error).length;
    $("pending-n").textContent = queue.length + (errs ? ` · ${errs} con error` : "");
    $("sync-badge").classList.toggle("warn", queue.length > 0 && !errs);
    $("sync-badge").classList.toggle("err", errs > 0);
  }
  function setNet() {
    const on = navigator.onLine;
    $("net").classList.toggle("off", !on);
    $("net-txt").textContent = on ? "en línea" : "sin red";
  }
  // Pendientes por módulo (círculo rojo): en la pestaña, el total junto al
  // título y, donde el sistema lo permite, en el ícono de la app instalada
  // (Badging API: Chrome/Edge de escritorio; en Android no suele mostrarse).
  const badges = {};
  function setBadge(moduleId, n) {
    badges[moduleId] = Math.max(0, n | 0);
    const tab = $("tab-n-" + moduleId);
    if (tab) { tab.textContent = badges[moduleId]; tab.classList.toggle("hidden", !badges[moduleId]); }
    const total = Object.values(badges).reduce((a, b) => a + b, 0);
    const t = $("total-n");
    t.textContent = total; t.classList.toggle("hidden", !total);
    try {
      if (total && navigator.setAppBadge) navigator.setAppBadge(total).catch(() => {});
      else if (!total && navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
    } catch (e) { /* sin soporte */ }
  }

  function show(id) {
    const mod = byId[id] || modules[0];
    if (!mod) return;
    active = mod.id;
    lsSet("terreno.tab", mod.id);
    for (const m of modules) {
      $("mod-" + m.id).classList.toggle("hidden", m.id !== mod.id);
      const b = $("tab-" + m.id); if (b) b.classList.toggle("on", m.id === mod.id);
    }
    // onShow es el lugar de lo que ocurre "al entrar": p. ej. la ventana de
    // seguridad del ácido de Ensilaje (que vive en index.html, no en un .js).
    if (mod.onShow) safe(mod, "onShow", () => mod.onShow());
  }

  function buildShell() {
    const tabs = $("tabs"), main = $("modules");
    tabs.innerHTML = modules.map((m) => `<button id="tab-${m.id}" data-tab="${m.id}">${esc(m.label)}<span class="dot-n hidden" id="tab-n-${m.id}"></span></button>`).join("");
    tabs.classList.toggle("hidden", modules.length < 2);
    main.innerHTML = modules.map((m) => `<section class="mod hidden" id="mod-${m.id}"></section>`).join("");
  }

  function wireEvents() {
    $("tabs").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-tab]"); if (b) show(b.dataset.tab);
    });
    $("sync-badge").addEventListener("click", async () => {
      if (!queue.length) { toast("Todo subido", "ok"); refreshAll(); return; }
      await sync();
      const errs = queue.filter((q) => q.error);
      if (!queue.length) toast("Todo subido", "ok");
      else if (errs.length) toast("No se pudo subir: " + errs[0].error, "err");
      else toast("Sin conexión con la base", "err");
    });
    window.addEventListener("online", () => { setNet(); sync(); refreshAll(); });
    window.addEventListener("offline", setNet);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") { sync(); refreshAll(); }
    });
  }

  function refreshAll() {
    for (const m of modules) if (m.refresh) safe(m, "refresh", () => m.refresh({}));
  }

  const ctx = {
    idbGet, idbPut, idbDel, toast, esc, uuid, fmtKg, fmtWhen, parseNum, localISO, todayStr,
    fetchJSON, isNetErr,
    enqueue: (moduleId, payload) => enqueue(moduleId, payload),
    pendingFor, confirm: confirmBox, setBadge: (id, n) => setBadge(id, n),
    persistent: () => canPersist,
  };

  async function start() {
    window.addEventListener("error", (e) => toast("Error: " + (e.message || "script"), "err"));
    window.addEventListener("unhandledrejection", (e) =>
      toast("Error: " + ((e.reason && e.reason.message) || e.reason || "async"), "err"));
    $("app-ver").textContent = APP_VERSION;

    // Botones primero, siempre: aunque falle el almacenamiento (lección Getac).
    buildShell();
    wireEvents();
    setNet();

    try {
      _db = await openDB();
      queue = await idbAll("queue");
    } catch (e) {
      _db = null;
      toast("Sin almacenamiento local: " + (e.message || e), "err");
    }
    canPersist = (await idbPut("meta", { t: Date.now() }, "__probe__")) === true;
    $("nopersist").classList.toggle("hidden", canPersist);
    renderBadges();

    for (const m of modules) {
      await safe(m, "init", () => m.init(ctx, $("mod-" + m.id)));
    }
    show(lsGet("terreno.tab") || (modules[0] && modules[0].id));
    refreshAll();
    sync();
    setInterval(() => {
      if (document.visibilityState === "visible" && navigator.onLine) { sync(); refreshAll(); }
    }, TICK_MS);

    if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  }

  return { register, start, ctx };
})();
