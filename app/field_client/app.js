"use strict";
/* Captura O2 — PWA de terreno. Vanilla JS, offline-first.
   Habla con /api/field/v1 (bootstrap + oxygen-readings). */

const API = "/api/field/v1";
const LS_ROUND = "captura.roundStart";

// localStorage puede estar bloqueado (WebViews/kioscos con datos de sitio
// restringidos lanzan "Access denied"): fallback a memoria para no romper.
const _mem = {};
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return (k in _mem) ? _mem[k] : null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { _mem[k] = v; } }

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
// Best-effort: estas funciones NUNCA rechazan. Si el equipo deniega la BD
// (abre pero falla al leer/escribir), resuelven en silencio y la app sigue
// en memoria. Así el almacenamiento local no puede romper el flujo.
function tx(store, mode) { return _db.transaction(store, mode).objectStore(store); }
function idbGet(store, key) {
  if (!_db) return Promise.resolve(undefined);
  return new Promise((res) => {
    try { const r = tx(store, "readonly").get(key); r.onsuccess = () => res(r.result); r.onerror = () => res(undefined); }
    catch (e) { res(undefined); }
  });
}
function idbPut(store, val, key) {
  if (!_db) return Promise.resolve(false);
  return new Promise((res) => {
    try { const r = tx(store, "readwrite").put(val, key); r.onsuccess = () => res(true); r.onerror = () => res(false); }
    catch (e) { res(false); }
  });
}
function idbDel(store, key) {
  if (!_db) return Promise.resolve();
  return new Promise((res) => {
    try { const r = tx(store, "readwrite").delete(key); r.onsuccess = () => res(); r.onerror = () => res(); }
    catch (e) { res(); }
  });
}
function idbAll(store) {
  if (!_db) return Promise.resolve([]);
  return new Promise((res) => {
    try { const r = tx(store, "readonly").getAll(); r.onsuccess = () => res(r.result || []); r.onerror = () => res([]); }
    catch (e) { res([]); }
  });
}

// ---------------------------------------------------------------------------
// Estado
// ---------------------------------------------------------------------------
let _canPersist = false; // ¿el equipo permite guardado local persistente?
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

// Acciones correctivas (fallback si un bootstrap viejo no las trae).
const DEFAULT_ACTIONS = [
  "Encendí aireación / oxígeno",
  "Revisé / ajusté flujo de agua",
  "Avisé al supervisor",
  "Segunda lectura / reingreso",
  "A verificar (aún sin acción)",
];
const SEV = { ok: 0, alerta: 1, alarma: 2 };
function worstLevel(...ls) { let o = "ok"; for (const l of ls) if (SEV[l] > SEV[o]) o = l; return o; }

// Rangos físicamente plausibles (espejo de water_quality.oxygen_range_issues).
// Fuera de estos límites la lectura es casi seguro un glitch/typo: se pide
// confirmación antes de guardar (no se bloquea). La temp alta REAL no se valida
// acá (es alarma biológica, ver th.water_temp), solo el glitch imposible.
const RANGE = { do: [0, 20], temp: [0, 45], sat: [0, 150] };

// ¿Temperatura un glitch de sensor (fuera del rango físico)? No confiable.
function tempGlitch(t) { return t != null && isFinite(t) && (t < RANGE.temp[0] || t > RANGE.temp[1]); }

// Severidad de la lectura: peor entre O2 absoluto (mg/L), saturación (%) y
// temperatura alta (°C). El mg/L dispara aunque falte temp. Si la temp es glitch,
// no se usa la saturación teórica ni la alarma de temp.
function evaluateLevel(doV, effTemp) {
  const alt = state.boot && state.boot.site ? state.boot.site.altitude_m : 170;
  const th = state.boot ? state.boot.thresholds : {};
  const glitch = tempGlitch(effTemp);
  let sat = null;
  if (isFinite(doV) && effTemp != null && isFinite(effTemp) && !glitch)
    sat = expectedSaturationPct(doV, effTemp, alt);
  const doLvl = isFinite(doV) ? evalThreshold(doV, th.o2_do_mg_l) : "ok";
  const satLvl = sat != null ? evalThreshold(sat, th.o2_saturation) : "ok";
  const tempLvl = (effTemp != null && isFinite(effTemp) && !glitch) ? evalThreshold(effTemp, th.water_temp) : "ok";
  const o2Lvl = worstLevel(doLvl, satLvl);
  const reason = (SEV[tempLvl] >= 2 && SEV[o2Lvl] >= 2) ? "Oxígeno bajo y temperatura alta"
               : (SEV[tempLvl] >= 2) ? "Temperatura muy alta" : "Oxígeno muy bajo";
  return { level: worstLevel(o2Lvl, tempLvl), sat: sat, o2Lvl: o2Lvl, tempLvl: tempLvl, reason: reason };
}

function rangeIssues(doV, tempV, satV) {
  const out = [];
  const chk = (v, lohi, label, unit) => {
    if (v == null || !isFinite(v)) return;
    if (v < lohi[0] || v > lohi[1]) out.push(`${label} ${v} ${unit} (rango ${lohi[0]}–${lohi[1]})`);
  };
  chk(doV, RANGE.do, "O₂", "mg/L");
  chk(tempV, RANGE.temp, "temperatura", "°C");
  chk(satV, RANGE.sat, "saturación", "%");
  return out;
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
  let v = lsGet(LS_ROUND);
  if (!v) { v = new Date().toISOString(); lsSet(LS_ROUND, v); }
  return v;
}
function newRound() { lsSet(LS_ROUND, new Date().toISOString()); recomputeDone(); renderDash(); toast("Nueva ronda iniciada"); }
function recomputeDone() {
  const start = roundStart();
  state.doneThisRound = {};
  for (const r of state.queue) {
    if (r.reading_datetime >= start) state.doneThisRound[r.pond_id] = true;
  }
  // marcas locales de lecturas ya sincronizadas en esta ronda
  const synced = JSON.parse(lsGet("captura.syncedRound") || "{}");
  for (const [pid, ts] of Object.entries(synced)) {
    if (ts >= start) state.doneThisRound[pid] = true;
  }
}
function markDoneLocal(pondId) {
  const synced = JSON.parse(lsGet("captura.syncedRound") || "{}");
  synced[pondId] = new Date().toISOString();
  lsSet("captura.syncedRound", JSON.stringify(synced));
}

// Temperatura MEDIDA de la ronda por unidad de cultivo. El primer estanque de
// una unidad en la ronda debe traer temperatura; los demás la heredan (laguna
// homogénea). Se guarda por unidad con timestamp; entradas anteriores al inicio
// de la ronda se ignoran (misma convención que syncedRound).
function getUnitRoundTemp(unitId) {
  if (unitId == null) return null;
  const start = roundStart();
  const temps = JSON.parse(lsGet("captura.roundTemps") || "{}");
  const e = temps[unitId];
  return (e && e.ts >= start && isFinite(e.temp)) ? e.temp : null;
}
function setUnitRoundTemp(unitId, temp) {
  if (unitId == null || !isFinite(temp)) return;
  const temps = JSON.parse(lsGet("captura.roundTemps") || "{}");
  const start = roundStart();
  const e = temps[unitId];
  // el primer estanque medido de la ronda fija la temperatura de la unidad
  if (!(e && e.ts >= start)) {
    temps[unitId] = { temp: temp, ts: new Date().toISOString() };
    lsSet("captura.roundTemps", JSON.stringify(temps));
  }
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

  // ¿Primer estanque de esta unidad en la ronda? -> temperatura obligatoria.
  const inheritTemp = getUnitRoundTemp(pond.cultivation_unit_id);
  const isFirst = inheritTemp == null;
  $("temp-req").classList.toggle("hidden", !isFirst);
  const hint = $("temp-hint");
  if (isFirst) {
    hint.className = "temp-hint";
    hint.textContent = "Primer estanque de la unidad: mide la temperatura (la heredan los demás).";
    hint.classList.remove("hidden");
    $("in-temp").placeholder = "0.0";
  } else {
    hint.className = "temp-hint inherit";
    hint.textContent = `Se heredará ${inheritTemp} °C de la ronda si la dejas vacía.`;
    hint.classList.remove("hidden");
    $("in-temp").placeholder = String(inheritTemp);
  }

  updateLive();
  show("view-cap");
  setTimeout(() => $("in-do").focus(), 100);
}
function updateLive() {
  const doV = parseFloat($("in-do").value.replace(",", "."));
  let tV = parseFloat($("in-temp").value.replace(",", "."));
  // sin temperatura escrita, previsualiza con la heredada de la ronda
  if (!isFinite(tV) && state.current) {
    const inh = getUnitRoundTemp(state.current.cultivation_unit_id);
    if (inh != null) tV = inh;
  }
  const r = evaluateLevel(doV, isFinite(tV) ? tV : null);
  const inDo = $("in-do"), inTemp = $("in-temp"), pill = $("live-alarm");
  inDo.classList.remove("sev-alerta", "sev-alarma");
  inTemp.classList.remove("sev-alerta", "sev-alarma");
  $("live-sat").textContent = r.sat != null ? r.sat.toFixed(1) + " %" : "—";
  if (!isFinite(doV) && !isFinite(tV)) {
    pill.className = "pill p-none"; pill.textContent = "—"; return;
  }
  // Dispara por O2 bajo (mg/L, aunque falte temp) o por temperatura alta.
  pill.className = "pill p-" + r.level;
  pill.textContent = r.level === "ok" ? "OK" : (r.level === "alerta" ? "Alerta" : "Alarma");
  if (r.o2Lvl !== "ok") inDo.classList.add(r.o2Lvl === "alarma" ? "sev-alarma" : "sev-alerta");
  if (r.tempLvl !== "ok") inTemp.classList.add(r.tempLvl === "alarma" ? "sev-alarma" : "sev-alerta");
}
async function saveReading() {
  const doV = parseFloat($("in-do").value.replace(",", "."));
  const tV = parseFloat($("in-temp").value.replace(",", "."));
  const unitId = state.current.cultivation_unit_id;
  const inheritTemp = getUnitRoundTemp(unitId);
  const isFirst = inheritTemp == null;

  if (!isFinite(doV)) { toast("Ingresa el O₂ disuelto", "err"); return; }
  // Primer estanque de la unidad en la ronda: temperatura obligatoria.
  if (isFirst && !isFinite(tV)) {
    toast("Mide la temperatura: es el primer estanque de la unidad en esta ronda", "err");
    $("in-temp").focus();
    return;
  }

  // Confirmación por valores fuera de rango físico (posible error de tipeo).
  const effTemp = isFinite(tV) ? tV : inheritTemp;
  const alt = state.boot && state.boot.site ? state.boot.site.altitude_m : 170;
  const satV = (isFinite(doV) && effTemp != null) ? expectedSaturationPct(doV, effTemp, alt) : null;
  const issues = rangeIssues(doV, isFinite(tV) ? tV : null, satV);
  if (issues.length && !confirm("Valor fuera de rango:\n· " + issues.join("\n· ") + "\n\n¿Guardar de todos modos?")) {
    return;
  }

  const rec = {
    client_uuid: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)),
    pond_id: state.current.id,
    reading_datetime: new Date().toISOString(),
    do_mg_l: doV,
    // Se envía solo la temperatura medida; el servidor hereda la de la ronda
    // cuando va vacía y marca water_temp_inherited.
    water_temp_c: isFinite(tV) ? tV : null,
    saturation_pct: null,
    observation: $("in-obs").value.trim() || null,
    acknowledged: false,
    corrective_action: null,
  };

  // Llamado a la acción: si la lectura está en alarma (O2 bajo o temp alta),
  // exige reconocerla y registrar la acción correctiva antes de guardar.
  const r = evaluateLevel(doV, effTemp);
  if (r.level === "alarma") {
    openAlarmModal(rec, r, doV, effTemp, isFinite(tV) ? tV : null, unitId);
    return;
  }
  commitReading(rec, isFinite(tV) ? tV : null, unitId);
}

// Persiste la lectura (memoria primero, luego best-effort a IndexedDB + sync).
function commitReading(rec, measuredTemp, unitId) {
  if (measuredTemp != null && isFinite(measuredTemp)) setUnitRoundTemp(unitId, measuredTemp);
  state.queue.push(rec);
  state.doneThisRound[rec.pond_id] = true;
  show("view-dash"); renderDash();
  idbPut("queue", rec);
  if (!_canPersist && !navigator.onLine) {
    toast("Guardado en memoria — sincroniza antes de cerrar la app", "err");
  } else {
    toast("Guardado" + (navigator.onLine ? ", sincronizando…" : " (offline)"), "ok");
  }
  if (navigator.onLine) syncQueue();
}

// --- Modal de alarma (camino 2: confirmar o corregir) ---
let _alarmRec = null, _alarmTemp = null, _alarmUnit = null, _alarmAction = null, _alarmReenterId = "in-do";
function openAlarmModal(rec, r, doV, effTemp, measuredTemp, unitId) {
  _alarmRec = rec; _alarmTemp = measuredTemp; _alarmUnit = unitId; _alarmAction = null;
  $("am-title").textContent = r.reason;
  const big = [];
  if (r.o2Lvl !== "ok") big.push("O₂ " + doV + " mg/L" + (r.sat != null ? " · " + r.sat.toFixed(0) + "% sat" : ""));
  if (r.tempLvl !== "ok") big.push("Temp " + effTemp + " °C");
  $("am-big").textContent = big.join("     ");
  $("am-confirm-val").textContent = "Confirmar la lectura";
  // "Digitar nuevo valor" vuelve al campo que disparó la alarma.
  _alarmReenterId = (r.tempLvl !== "ok" && r.o2Lvl === "ok") ? "in-temp" : "in-do";
  $("am-action-err").classList.remove("show");
  // Botones de acción (de bootstrap, o fallback)
  const acts = (state.boot && state.boot.corrective_actions) || DEFAULT_ACTIONS;
  const box = $("am-actions"); box.innerHTML = "";
  acts.forEach((a) => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = a;
    b.addEventListener("click", () => {
      _alarmAction = a;
      Array.from(box.children).forEach((c) => c.classList.toggle("on", c === b));
      $("am-action-err").classList.remove("show");
    });
    box.appendChild(b);
  });
  $("am-step-action").classList.add("hidden");     // arranca en el paso 1
  $("am-step-confirm").classList.remove("hidden");
  $("modal-alarm").classList.add("show");
}
function closeAlarmModal() { $("modal-alarm").classList.remove("show"); }
// Confirmar el valor -> revela la acción correctiva.
function alarmConfirmValue() {
  $("am-step-confirm").classList.add("hidden");
  $("am-step-action").classList.remove("hidden");
}
// Digitar nuevo valor -> cierra y vuelve al campo que disparó (al reintentar re-evalúa).
function alarmReenter() {
  closeAlarmModal();
  const el = $(_alarmReenterId); el.focus(); el.select();
}
// Guardar (tras elegir acción).
function alarmSave() {
  $("am-action-err").classList.toggle("show", !_alarmAction);
  if (!_alarmAction) return;
  _alarmRec.acknowledged = true;
  _alarmRec.corrective_action = _alarmAction;
  closeAlarmModal();
  commitReading(_alarmRec, _alarmTemp, _alarmUnit);
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
  $("am-confirm-val").addEventListener("click", alarmConfirmValue);
  $("am-reenter").addEventListener("click", alarmReenter);
  $("am-cancel").addEventListener("click", closeAlarmModal);
  $("am-save").addEventListener("click", alarmSave);
  $("modal-alarm").addEventListener("click", (e) => { if (e.target.id === "modal-alarm") closeAlarmModal(); });
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
    toast("Sin almacenamiento local: " + (e.message || e), "err");
  }

  // Sonda real: algunos equipos abren la BD pero deniegan escrituras.
  _canPersist = (await idbPut("meta", { t: Date.now() }, "__probe__")) === true;
  const np = document.getElementById("nopersist");
  if (np) np.classList.toggle("hidden", _canPersist);

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
