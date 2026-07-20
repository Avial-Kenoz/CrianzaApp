"use strict";
/* Captura O2 — PWA de terreno. Vanilla JS, offline-first.
   Habla con /api/field/v1 (bootstrap + oxygen-readings). */

const API = "/api/field/v1";
const LS_ROUND = "captura.roundStart";

// ---------------------------------------------------------------------------
// IndexedDB
// ---------------------------------------------------------------------------
let _db = null;
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("captura", 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta");
      if (!db.objectStoreNames.contains("queue")) db.createObjectStore("queue", { keyPath: "client_uuid" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
function tx(store, mode) { return _db.transaction(store, mode).objectStore(store); }
function idbGet(store, key) {
  if (!_db) return Promise.resolve(undefined);
  return new Promise((res, rej) => { const r = tx(store, "readonly").get(key); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
}
function idbPut(store, val, key) {
  if (!_db) return Promise.resolve();
  return new Promise((res, rej) => { const r = tx(store, "readwrite").put(val, key); r.onsuccess = () => res(); r.onerror = () => rej(r.error); });
}
function idbDel(store, key) {
  if (!_db) return Promise.resolve();
  return new Promise((res, rej) => { const r = tx(store, "readwrite").delete(key); r.onsuccess = () => res(); r.onerror = () => rej(r.error); });
}
function idbAll(store) {
  if (!_db) return Promise.resolve([]);
  return new Promise((res, rej) => { const r = tx(store, "readonly").getAll(); r.onsuccess = () => res(r.result || []); r.onerror = () => rej(r.error); });
}

// ---------------------------------------------------------------------------
// Estado
// ---------------------------------------------------------------------------
const state = {
  boot: null,          // {ponds, units, thresholds, site, server_time}
  pondsByCode: {},     // qr_code -> pond
  pondsById: {},       // id -> pond
  queue: [],           // lecturas pendientes de subir
  doneThisRound: {},   // pond_id -> true (según lecturas locales de la ronda)
  current: null,       // estanque en captura
};

// ---------------------------------------------------------------------------
// Química O2 (espejo del motor server-side, para feedback en vivo)
// ---------------------------------------------------------------------------
function doSaturationMgL(tempC, altitudeM) {
  const tK = tempC + 273.15;
  const lnC0 = -139.34411 + 1.575701e5 / tK - 6.642308e7 / Math.pow(tK, 2)
    + 1.243800e10 / Math.pow(tK, 3) - 8.621949e11 / Math.pow(tK, 4);
  const c0 = Math.exp(lnC0);
  const pb = Math.pow(1 - 2.25577e-5 * altitudeM, 5.25588);
  const pwv = Math.exp(11.8571 - 3840.70 / tK - 216961 / Math.pow(tK, 2));
  const theta = 0.000975 - 1.426e-5 * tempC + 6.436e-8 * tempC * tempC;
  const fp = ((pb - pwv) * (1 - theta * pb)) / ((1 - pwv) * (1 - theta));
  return c0 * fp;
}
function expectedSaturationPct(doMgL, tempC, altitudeM) {
  const cs = doSaturationMgL(tempC, altitudeM);
  return cs > 0 ? (doMgL / cs) * 100 : 0;
}
function evalThreshold(value, spec) {
  if (value == null || !spec) return "ok";
  const { alert, alarm, comparator } = spec;
  if (comparator === "lt") {
    if (alarm != null && value < alarm) return "alarma";
    if (alert != null && value < alert) return "alerta";
  } else {
    if (alarm != null && value > alarm) return "alarma";
    if (alert != null && value > alert) return "alerta";
  }
  return "ok";
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
function show(view) {
  ["view-dash", "view-cap", "view-scan"].forEach((v) => $(v).classList.toggle("hidden", v !== view));
}
let _toastT = null;
function toast(msg, kind) {
  const t = $("toast"); t.textContent = msg; t.className = "toast show " + (kind || "");
  clearTimeout(_toastT); _toastT = setTimeout(() => t.classList.remove("show"), 2600);
}
function setNet() {
  const on = navigator.onLine;
  $("net").className = "net" + (on ? "" : " off");
  $("net-txt").textContent = on ? "en línea" : "sin conexión";
}
function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// ---------------------------------------------------------------------------
// Ronda
// ---------------------------------------------------------------------------
function roundStart() {
  let v = localStorage.getItem(LS_ROUND);
  if (!v) { v = new Date().toISOString(); localStorage.setItem(LS_ROUND, v); }
  return v;
}
function newRound() { localStorage.setItem(LS_ROUND, new Date().toISOString()); recomputeDone(); renderDash(); toast("Nueva ronda iniciada"); }
function recomputeDone() {
  const start = roundStart();
  state.doneThisRound = {};
  for (const r of state.queue) {
    if (r.reading_datetime >= start) state.doneThisRound[r.pond_id] = true;
  }
  // marcas locales de lecturas ya sincronizadas en esta ronda
  const synced = JSON.parse(localStorage.getItem("captura.syncedRound") || "{}");
  for (const [pid, ts] of Object.entries(synced)) {
    if (ts >= start) state.doneThisRound[pid] = true;
  }
}
function markDoneLocal(pondId) {
  const synced = JSON.parse(localStorage.getItem("captura.syncedRound") || "{}");
  synced[pondId] = new Date().toISOString();
  localStorage.setItem("captura.syncedRound", JSON.stringify(synced));
}

// ---------------------------------------------------------------------------
// Render dashboard
// ---------------------------------------------------------------------------
function renderDash() {
  const ponds = state.boot ? state.boot.ponds : [];
  $("no-data").classList.toggle("hidden", ponds.length > 0);
  const done = ponds.filter((p) => state.doneThisRound[p.id]).length;
  $("prog-n").textContent = done + "/" + ponds.length;
  $("prog-bar").style.width = ponds.length ? (100 * done / ponds.length) + "%" : "0%";
  $("pending-n").textContent = state.queue.length;
  $("sync-badge").className = "badge" + (state.queue.length ? " warn" : "");
  $("boot-when").textContent = state.boot ? fmtWhen(state.boot._fetchedAt || state.boot.server_time) : "—";

  const list = $("pond-list");
  list.innerHTML = "";
  // pendientes primero
  const sorted = ponds.slice().sort((a, b) => {
    const da = state.doneThisRound[a.id] ? 1 : 0, db = state.doneThisRound[b.id] ? 1 : 0;
    if (da !== db) return da - db;
    return a.name.localeCompare(b.name);
  });
  for (const p of sorted) {
    const el = document.createElement("div");
    el.className = "pond" + (state.doneThisRound[p.id] ? " done" : "");
    el.innerHTML = `<span class="status"></span>
      <div class="info"><div class="pn"></div><div class="un"></div></div>
      <span class="chev">›</span>`;
    el.querySelector(".pn").textContent = p.name;
    el.querySelector(".un").textContent = p.cultivation_unit_name || "";
    el.addEventListener("click", () => openCapture(p));
    list.appendChild(el);
  }
}

// ---------------------------------------------------------------------------
// Captura
// ---------------------------------------------------------------------------
function openCapture(pond) {
  state.current = pond;
  $("cap-pn").textContent = pond.name;
  $("cap-un").textContent = pond.cultivation_unit_name || "";
  $("in-do").value = ""; $("in-temp").value = ""; $("in-obs").value = "";
  updateLive();
  show("view-cap");
  setTimeout(() => $("in-do").focus(), 100);
}
function updateLive() {
  const doV = parseFloat($("in-do").value.replace(",", "."));
  const tV = parseFloat($("in-temp").value.replace(",", "."));
  const alt = state.boot && state.boot.site ? state.boot.site.altitude_m : 170;
  const th = state.boot ? state.boot.thresholds : {};
  if (isFinite(doV) && isFinite(tV)) {
    const sat = expectedSaturationPct(doV, tV, alt);
    $("live-sat").textContent = sat.toFixed(1) + " %";
    const lvl = evalThreshold(sat, th.o2_saturation);
    const pill = $("live-alarm");
    pill.className = "pill p-" + lvl;
    pill.textContent = lvl === "ok" ? "OK" : (lvl === "alerta" ? "Alerta" : "Alarma");
  } else {
    $("live-sat").textContent = "—";
    $("live-alarm").className = "pill p-none"; $("live-alarm").textContent = "—";
  }
}
async function saveReading() {
  const doV = parseFloat($("in-do").value.replace(",", "."));
  const tV = parseFloat($("in-temp").value.replace(",", "."));
  if (!isFinite(doV) && !isFinite(tV)) { toast("Ingresa al menos O₂ o temperatura", "err"); return; }
  const rec = {
    client_uuid: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)),
    pond_id: state.current.id,
    reading_datetime: new Date().toISOString(),
    do_mg_l: isFinite(doV) ? doV : null,
    water_temp_c: isFinite(tV) ? tV : null,
    saturation_pct: null,
    observation: $("in-obs").value.trim() || null,
  };
  await idbPut("queue", rec);
  state.queue.push(rec);
  state.doneThisRound[rec.pond_id] = true;
  show("view-dash"); renderDash();
  toast("Guardado" + (navigator.onLine ? ", sincronizando…" : " (offline)"), "ok");
  if (navigator.onLine) syncQueue();
}

// ---------------------------------------------------------------------------
// Escáner: detección sobre canvas. Usa BarcodeDetector si soporta qr_code;
// si no, jsQR (window.jsQR) cuando esté disponible. Fallback final: la lista.
// ---------------------------------------------------------------------------
let _stream = null, _scanning = false, _scanTimer = null;

async function makeDetector() {
  if ("BarcodeDetector" in window) {
    try {
      const fmts = await BarcodeDetector.getSupportedFormats();
      if (fmts && fmts.includes("qr_code")) {
        const bd = new BarcodeDetector({ formats: ["qr_code"] });
        return async (canvas) => {
          const codes = await bd.detect(canvas);
          return codes && codes.length ? codes[0].rawValue : null;
        };
      }
    } catch (e) { /* sigue con jsQR */ }
  }
  if (typeof window.jsQR === "function") {
    return (canvas, ctx, w, h) => {
      const img = ctx.getImageData(0, 0, w, h);
      const r = window.jsQR(img.data, w, h, { inversionAttempts: "attemptBoth" });
      return r && r.data ? r.data : null;
    };
  }
  return null;
}

async function startScan() {
  const detect = await makeDetector();
  if (!detect) {
    toast("Escáner no disponible en este teléfono; toca el estanque en la lista", "err");
    return;
  }
  try {
    _stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
  } catch (e) {
    toast("No se pudo abrir la cámara; usa la lista", "err"); return;
  }
  const video = $("video");
  video.setAttribute("playsinline", "");
  video.srcObject = _stream;
  try { await video.play(); } catch (e) { /* algunos navegadores reproducen solo */ }
  show("view-scan");

  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  _scanning = true;
  _scanTimer = setInterval(async () => {
    if (!_scanning) return;
    const w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return;                 // frame aún sin dimensiones
    canvas.width = w; canvas.height = h;
    ctx.drawImage(video, 0, 0, w, h);
    try {
      const raw = await detect(canvas, ctx, w, h);
      if (raw) onScanned(raw);
    } catch (e) { /* frame sin código */ }
  }, 220);
}

function stopScan() {
  _scanning = false;
  if (_scanTimer) { clearInterval(_scanTimer); _scanTimer = null; }
  if (_stream) { _stream.getTracks().forEach((t) => t.stop()); _stream = null; }
}

function onScanned(raw) {
  const code = (raw || "").trim().toUpperCase();
  const pond = state.pondsByCode[code];
  stopScan();
  if (pond) { openCapture(pond); }
  else { show("view-dash"); toast("QR no reconocido: " + code, "err"); }
}

// ---------------------------------------------------------------------------
// Sincronización
// ---------------------------------------------------------------------------
async function refreshBootstrap() {
  if (!navigator.onLine) { toast("Sin conexión para actualizar datos", "err"); return; }
  try {
    const r = await fetch(API + "/bootstrap", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    data._fetchedAt = new Date().toISOString();
    state.boot = data;
    indexPonds();
    await idbPut("meta", data, "boot");
    renderDash();
    toast("Datos actualizados (" + data.ponds.length + " estanques)", "ok");
  } catch (e) {
    toast("No se pudo actualizar: " + e.message, "err");
  }
}
function indexPonds() {
  state.pondsByCode = {}; state.pondsById = {};
  if (!state.boot) return;
  for (const p of state.boot.ponds) {
    if (p.qr_code) state.pondsByCode[p.qr_code.toUpperCase()] = p;
    state.pondsById[p.id] = p;
  }
}
let _syncing = false;
async function syncQueue() {
  if (_syncing || !navigator.onLine || state.queue.length === 0) return;
  _syncing = true;
  try {
    const readings = state.queue.map((r) => r);
    const r = await fetch(API + "/oxygen-readings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ readings }),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const res = await r.json();
    // created + duplicate = ya están en el servidor -> sacar de la cola
    const settled = new Set();
    for (const it of res.results) {
      if (it.status === "created" || it.status === "duplicate") settled.add(it.client_uuid);
    }
    for (const uuid of settled) { await idbDel("queue", uuid); }
    const startRound = roundStart();
    for (const rr of state.queue) {
      if (settled.has(rr.client_uuid) && rr.reading_datetime >= startRound) markDoneLocal(rr.pond_id);
    }
    state.queue = state.queue.filter((rr) => !settled.has(rr.client_uuid));
    renderDash();
    const errN = res.errors || 0;
    toast(`Sincronizado: ${res.created} nuevas` + (errN ? `, ${errN} con error` : ""), errN ? "err" : "ok");
  } catch (e) {
    toast("Sincronización pendiente: " + e.message, "err");
  } finally {
    _syncing = false;
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
function wireEvents() {
  $("btn-scan").addEventListener("click", startScan);
  $("btn-scan-cancel").addEventListener("click", () => { stopScan(); show("view-dash"); });
  $("btn-sync").addEventListener("click", syncQueue);
  $("btn-refresh").addEventListener("click", refreshBootstrap);
  $("btn-newround").addEventListener("click", newRound);
  $("btn-cancel").addEventListener("click", () => show("view-dash"));
  $("btn-save").addEventListener("click", saveReading);
  $("in-do").addEventListener("input", updateLive);
  $("in-temp").addEventListener("input", updateLive);
  window.addEventListener("online", () => { setNet(); syncQueue(); });
  window.addEventListener("offline", setNet);
}

async function init() {
  // Errores no capturados -> visibles (diagnóstico en dispositivos sin devtools)
  window.addEventListener("error", (e) => toast("Error: " + (e.message || "script"), "err"));
  window.addEventListener("unhandledrejection", (e) =>
    toast("Error: " + ((e.reason && e.reason.message) || e.reason || "async"), "err"));

  // Enlaza botones SIEMPRE, aunque el almacenamiento local falle.
  wireEvents();
  setNet();

  // Almacenamiento local tolerante a fallos: sin IndexedDB, sigue en modo online.
  try {
    _db = await openDB();
    state.queue = await idbAll("queue");
    const boot = await idbGet("meta", "boot");
    if (boot) { state.boot = boot; indexPonds(); }
  } catch (e) {
    _db = null;
    toast("Sin almacenamiento local (modo online): " + (e.message || e), "err");
  }

  roundStart();
  recomputeDone();
  renderDash();

  if (!state.boot && navigator.onLine) refreshBootstrap();
  if (navigator.onLine) syncQueue();

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
}
init();
