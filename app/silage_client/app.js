/* PWA de Molienda y Ensilaje (registro PC 03.2).
 *
 * Captura offline en la zona de molienda: cada CARGA se guarda en una cola
 * local (IndexedDB) y se sincroniza cuando vuelve la señal. La captura es
 * ADITIVA: volver a registrar el mismo día suma kilos, no reemplaza. Por eso la
 * pantalla de inicio muestra siempre el acumulado del día y el tambor abierto,
 * para que el operador que vuelve por segunda vez agregue en vez de sumar
 * mentalmente y reescribir el total.
 *
 * El FOLIO lo asigna el servidor al sincronizar: acá se muestra "pendiente"
 * hasta que llega. Mostrar un número provisorio llevaría al operador a anotar en
 * su cuaderno un folio que después no existe.
 */

const API = "/api/silage/v1";
const LS_OPERATOR = "silage_operator";

// --- almacenamiento tolerante (Safari privado rompe localStorage) -----------
const _mem = {};
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return (k in _mem) ? _mem[k] : null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { _mem[k] = v; } }

// --- IndexedDB: meta (bootstrap cacheado) + queue (cargas por sincronizar) --
let _db = null, _canPersist = true;
function openDB() {
  return new Promise((resolve) => {
    const req = indexedDB.open("ensilaje", 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta");
      if (!db.objectStoreNames.contains("queue")) db.createObjectStore("queue", { keyPath: "client_uuid" });
    };
    req.onsuccess = () => { _db = req.result; resolve(true); };
    req.onerror = () => { _canPersist = false; resolve(false); };
  });
}
function tx(store, mode) { return _db.transaction(store, mode).objectStore(store); }
function idbGet(store, key) {
  return new Promise((res) => {
    if (!_db) return res(null);
    const r = tx(store, "readonly").get(key);
    r.onsuccess = () => res(r.result ?? null); r.onerror = () => res(null);
  });
}
function idbPut(store, val, key) {
  return new Promise((res) => {
    if (!_db) return res(false);
    const r = key === undefined ? tx(store, "readwrite").put(val) : tx(store, "readwrite").put(val, key);
    r.onsuccess = () => res(true); r.onerror = () => res(false);
  });
}
function idbDel(store, key) {
  return new Promise((res) => {
    if (!_db) return res(false);
    const r = tx(store, "readwrite").delete(key);
    r.onsuccess = () => res(true); r.onerror = () => res(false);
  });
}
function idbAll(store) {
  return new Promise((res) => {
    if (!_db) return res([]);
    const r = tx(store, "readonly").getAll();
    r.onsuccess = () => res(r.result || []); r.onerror = () => res([]);
  });
}

// --- estado ----------------------------------------------------------------
let BOOT = { thresholds: {}, open_drum: null, operators: [], acid_available_lts: null, recent_events: [] };
let QUEUE = [];        // cargas locales sin confirmar
let SYNCED = [];       // cargas ya confirmadas por el servidor (para ver el día)

const $ = (id) => document.getElementById(id);

function show(view) {
  ["v-home", "v-form", "v-close"].forEach((v) => $(v).classList.toggle("hidden", v !== view));
  window.scrollTo(0, 0);
}
let _toastTimer = null;
function toast(msg, kind) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast show " + (kind || "");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}
function setNet() {
  const on = navigator.onLine;
  $("net").className = "net" + (on ? "" : " off");
  $("net-txt").textContent = on ? "en línea" : "sin conexión";
}
function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function num(v) {
  if (v === null || v === undefined) return null;
  const s = String(v).trim().replace(",", ".");
  if (s === "") return null;
  const n = Number(s);
  return isFinite(n) ? n : null;
}
function fmtKg(n) {
  return (Math.round((n || 0) * 100) / 100).toLocaleString("es-CL", { maximumFractionDigits: 2 });
}
function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0, v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}
function thr(key, dflt) {
  const v = BOOT.thresholds ? BOOT.thresholds[key] : undefined;
  return (v === undefined || v === null) ? dflt : Number(v);
}

// --- vista de hoy: cola local + confirmadas del servidor --------------------
function todayLoads() {
  const day = todayISO();
  const local = QUEUE.filter((q) => q.log_date === day)
    .map((q) => ({ ...q, _pending: true }));
  const remote = SYNCED.filter((e) => e.log_date === day && !QUEUE.some((q) => q.client_uuid === e.client_uuid))
    .map((e) => ({ ...e, _pending: false }));
  return [...remote, ...local].sort((a, b) =>
    String(a.event_datetime).localeCompare(String(b.event_datetime)));
}

/* Tambor abierto según la app, que puede ir un paso adelante del servidor: si hay
 * un cierre esperando en la cola offline, para el operador ese tambor YA está
 * cerrado y no debe seguir ofreciéndose. */
function effectiveOpenDrum() {
  const drum = BOOT.open_drum;
  if (!drum) return null;
  const cierrePendiente = QUEUE.some(
    (q) => q._kind === "cierre" && q.drum_number === drum.drum_number);
  return cierrePendiente ? null : drum;
}

/* N° que se propone para el próximo tambor: el menor de los vacíos registrados
 * (el procedimiento manda seguir el correlativo). Si no hay ninguno dado de alta,
 * se sugiere el siguiente al que se acaba de cerrar. */
function suggestedDrumNumber() {
  const abiertos = effectiveOpenDrum();
  if (abiertos) return abiertos.drum_number;
  const libres = (BOOT.available_drums || []).map((d) => d.drum_number).sort((a, b) => a - b);
  if (libres.length) return libres[0];
  const cerrado = BOOT.open_drum ? BOOT.open_drum.drum_number : null;
  return cerrado ? cerrado + 1 : "";
}

function renderHome() {
  // Tambor abierto y su llenado (la capacidad es única para todos los tambores)
  const drum = effectiveOpenDrum();
  const cap = thr("drum_capacity_kg", 200);
  const pendingKg = QUEUE.filter((q) => q.had_mortality && q.drum_number === (drum && drum.drum_number))
    .reduce((a, q) => a + (q.mortality_kg || 0), 0);
  const kg = (drum ? Number(drum.total_kg || 0) : 0) + pendingKg;

  $("drum-n").textContent = drum ? ("N° " + drum.drum_number) : "—";
  $("drum-kg").textContent = fmtKg(kg);
  $("drum-cap").textContent = "capacidad " + fmtKg(cap) + " kg";
  const pct = cap > 0 ? Math.min(100, (kg / cap) * 100) : 0;
  // El procedimiento manda cerrar y sellar a 4/5 de la capacidad: el aviso es una
  // instrucción, no una sugerencia.
  const closeAt = cap * thr("drum_close_fill_ratio", 0.8);
  const bar = $("drum-bar");
  bar.className = "bar" + (kg >= cap ? " over" : (kg >= closeAt ? " near" : ""));
  bar.firstElementChild.style.width = pct + "%";
  $("drum-state").textContent = !drum ? "abre uno al registrar"
    : (kg >= closeAt ? "cerrar y sellar" : "");
  $("b-drum").textContent = drum ? ("Tambor " + drum.drum_number) : "Sin tambor abierto";
  $("b-drum").className = "badge" + (kg >= closeAt ? " warn" : "");
  // Cerrar sin carga solo tiene sentido si hay un tambor abierto.
  $("btn-close").disabled = !drum;

  // Acumulado del día: lo que hace visible el modelo aditivo
  const loads = todayLoads();
  const withMortality = loads.filter((l) => l.had_mortality && !l.voided_at);
  const totalKg = withMortality.reduce((a, l) => a + (Number(l.mortality_kg) || 0), 0);
  $("today-kg").textContent = fmtKg(totalKg) + " kg";
  const noMort = loads.some((l) => !l.had_mortality && !l.voided_at);
  $("today-lb").textContent = withMortality.length
    ? `hoy · ${withMortality.length} carga${withMortality.length > 1 ? "s" : ""}`
    : (noMort ? "hoy · declarado sin mortalidad" : "hoy · sin cargas registradas");

  // Ácido disponible
  const acid = BOOT.acid_available_lts;
  const low = acid !== null && acid !== undefined && acid < thr("acid_low_stock_lts", 50);
  $("b-acid").textContent = (acid === null || acid === undefined) ? "Ácido —" : ("Ácido " + fmtKg(acid) + " lts");
  $("b-acid").className = "badge" + (low ? " warn" : "");
  $("b-queue").textContent = QUEUE.length;

  // Lista de cargas del día
  const list = $("today-list");
  if (!loads.length) {
    list.innerHTML = '<div class="empty">Todavía no registras cargas hoy.</div>';
    return;
  }
  list.innerHTML = loads.map((l) => {
    const t = String(l.event_datetime || "").slice(11, 16);
    const folio = l._pending ? "folio pendiente" : (l.folio_display || "");
    if (l._kind === "cierre") {
      return `<div class="load ${l._pending ? "pending" : ""}">
        <div class="info"><div class="l1">Tambor ${l.drum_number} cerrado</div>
        <div class="l2">${t}${l.seal_note ? " · " + l.seal_note : ""}${l._pending ? " · por sincronizar" : ""}</div></div></div>`;
    }
    if (!l.had_mortality) {
      return `<div class="load ${l._pending ? "pending" : ""}">
        <div class="info"><div class="l1">Sin mortalidad</div>
        <div class="l2">${t} · <span class="folio">${folio}</span></div></div></div>`;
    }
    const ph = l.ph_value != null ? ("pH " + l.ph_value) : "sin pH";
    const acidTxt = l.acid_lts != null ? (" · " + fmtKg(l.acid_lts) + " lts") : "";
    return `<div class="load ${l._pending ? "pending" : ""} ${l.voided_at ? "voided" : ""}">
      <div class="info">
        <div class="l1">${fmtKg(l.mortality_kg)} kg · tambor ${l.drum_number ?? "—"}${l.drum_closed ? " (cerrado)" : ""}</div>
        <div class="l2">${t} · ${ph}${acidTxt} · <span class="folio">${folio}</span></div>
      </div>
    </div>`;
  }).join("");
}

// --- formulario -------------------------------------------------------------
function openForm() {
  $("f-kg").value = "";
  $("f-acid").value = "";
  $("f-ph").value = "";
  $("f-additional").checked = false;
  $("f-closed").checked = false;
  $("f-obs").value = "";
  // Si el tambor abierto se acaba de cerrar, se propone el siguiente correlativo
  // en vez del que ya está sellado.
  $("f-drum").value = suggestedDrumNumber();

  const sel = $("f-operator");
  const saved = lsGet(LS_OPERATOR);
  sel.innerHTML = '<option value="">—</option>' +
    (BOOT.operators || []).map((o) => `<option value="${o.id}">${o.name}</option>`).join("");
  if (saved) sel.value = saved;

  liveCheck();
  show("v-form");
  setTimeout(() => $("f-kg").focus(), 60);
}

/* Avisos en vivo. Regla que sostiene el diseño: la dosis fuera de rango es un
 * AVISO de verosimilitud (para cazar tipeos), nunca un error — el criterio de
 * aceptación es el pH. Si se marcara en rojo como error, el operador terminaría
 * "ajustando" los litros digitados para que calcen, corrompiendo el dato que
 * alimenta el inventario de ácido. */
function liveCheck() {
  const kg = num($("f-kg").value);
  const acid = num($("f-acid").value);
  const ph = num($("f-ph").value);
  const phLimit = thr("ph_limit", 4);
  const msgs = [];
  let cls = "hint";

  // Referencia del procedimiento (1 lt por 4 kg). Se MUESTRA, no se rellena: el
  // número que consume el inventario debe ser el que el operador usó de verdad.
  if (kg) {
    const nominal = kg * thr("acid_lts_per_kg_nominal", 0.25);
    msgs.push(`El procedimiento indica ~${fmtKg(nominal)} lts para ${fmtKg(kg)} kg (1 lt por 4 kg). Digita los que hayas usado.`);
  }
  if (kg && acid) {
    const dose = acid / kg;
    const lo = thr("acid_lts_per_kg_min", 0.20), hi = thr("acid_lts_per_kg_max", 0.30);
    if (dose < lo || dose > hi) {
      msgs.push(`Dosis ${dose.toFixed(3)} lts/kg fuera de lo habitual (${lo}–${hi}). Revisa los litros; si el pH quedó bajo ${phLimit}, la carga está conforme igual.`);
      cls = "hint warn";
    }
  }
  if (ph !== null && ph >= phLimit) {
    msgs.push(`El pH ${ph} no bajó de ${phLimit}. Vierte más ácido y registra cuando lo hayas logrado; si lo dejas así, la carga queda como no conforme.`);
    cls = "hint bad";
  }
  const cap = thr("drum_capacity_kg", 200);
  const drumKg = BOOT.open_drum ? Number(BOOT.open_drum.total_kg || 0) : 0;
  if (kg && drumKg + kg >= cap * thr("drum_close_fill_ratio", 0.8) && !$("f-closed").checked) {
    msgs.push(`Con esta carga el tambor llega a ${fmtKg(drumKg + kg)} de ${fmtKg(cap)} kg (4/5): el procedimiento manda cerrarlo y sellarlo.`);
    if (cls === "hint") cls = "hint warn";
  }
  const h = $("hint-dose");
  h.className = cls;
  h.innerHTML = msgs.join("<br>");
}

async function saveLoad() {
  const kg = num($("f-kg").value);
  const drum = num($("f-drum").value);
  const acid = num($("f-acid").value);
  const ph = num($("f-ph").value);

  if (kg === null || kg <= 0) return toast("Falta el peso de la mortalidad", "err");
  if (drum === null || drum <= 0) return toast("Falta el número de tambor", "err");

  const operator = $("f-operator").value;
  if (operator) lsSet(LS_OPERATOR, operator);

  const rec = {
    client_uuid: uuid(),
    event_datetime: new Date().toISOString(),
    log_date: todayISO(),
    had_mortality: true,
    mortality_kg: kg,
    acid_used: acid !== null && acid > 0,
    acid_lts: acid,
    additional_acid: $("f-additional").checked,
    ph_value: ph,
    drum_number: drum,
    drum_closed: $("f-closed").checked,
    operator_id: operator ? Number(operator) : null,
    observation: $("f-obs").value.trim() || null,
  };
  await enqueue(rec);
  show("v-home");
}

async function openNoMortality() {
  const already = todayLoads().some((l) => !l.voided_at);
  if (already && !confirm("Ya hay registros de hoy. ¿Marcar igual que no hubo mortalidad?")) return;
  const operator = lsGet(LS_OPERATOR);
  await enqueue({
    client_uuid: uuid(),
    event_datetime: new Date().toISOString(),
    log_date: todayISO(),
    had_mortality: false,
    mortality_kg: null,
    acid_used: false,
    acid_lts: null,
    additional_acid: false,
    ph_value: null,
    drum_number: null,
    drum_closed: false,
    operator_id: operator ? Number(operator) : null,
    observation: null,
  });
}

// --- cerrar el tambor sin carga --------------------------------------------
/* Caso que no cubría la casilla "cerré el tambor con esta carga": el tambor va
 * casi lleno, la molienda del día NO cabe, y el operador la echa en un tambor
 * nuevo dando por terminado el anterior. Acá el tambor se cierra sin recibir
 * nada, así que no hay carga de la que deducir quién lo cerró. */
function openClose() {
  const drum = effectiveOpenDrum();
  if (!drum) return toast("No hay ningún tambor abierto", "err");

  const cap = thr("drum_capacity_kg", 200);
  const pendingKg = QUEUE.filter((q) => q.had_mortality && q.drum_number === drum.drum_number)
    .reduce((a, q) => a + (q.mortality_kg || 0), 0);
  const kg = Number(drum.total_kg || 0) + pendingKg;
  const pct = cap > 0 ? Math.round((kg / cap) * 100) : 0;

  $("c-drum").textContent = "N° " + drum.drum_number;
  $("c-kg").textContent = fmtKg(kg) + " kg de " + fmtKg(cap) + " kg (" + pct + "%)";
  $("c-note").value = "";
  $("c-by").value = "";
  const libres = (BOOT.available_drums || []).map((d) => d.drum_number).sort((a, b) => a - b);
  $("c-next").textContent = libres.length
    ? ("El siguiente será el N° " + libres[0] + ".")
    : "No hay tambores vacíos dados de alta: avisa para registrar la próxima tanda.";
  show("v-close");
}

async function confirmClose() {
  const drum = effectiveOpenDrum();
  if (!drum) { show("v-home"); return toast("No hay ningún tambor abierto", "err"); }

  await enqueue({
    _kind: "cierre",
    client_uuid: uuid(),
    event_datetime: new Date().toISOString(),
    log_date: todayISO(),
    drum_number: drum.drum_number,
    sealed_by: $("c-by").value.trim() || null,
    seal_note: $("c-note").value.trim() || null,
  });
  show("v-home");
}

async function enqueue(rec) {
  QUEUE.push(rec);
  await idbPut("queue", rec);
  renderHome();
  if (!_canPersist && !navigator.onLine) {
    toast("Sin almacenamiento local: no cierres la app hasta sincronizar", "warn");
  } else {
    toast("Guardado" + (navigator.onLine ? ", sincronizando…" : " (offline)"), "ok");
  }
  if (navigator.onLine) syncQueue();
}

// --- sincronización ---------------------------------------------------------
/* La cola lleva DOS tipos de acción: cargas (_kind ausente o "carga") y cierres
 * de tambor (_kind "cierre"). Se sincronizan en ORDEN CRONOLÓGICO, agrupando
 * solo las cargas consecutivas en un lote.
 *
 * El orden no es un detalle estético: si el operador cierra el tambor 6 y luego
 * registra una carga al 7, mandar la carga primero la haría rechazar ("hay otro
 * tambor abierto"). Por eso no se envía todo junto ni se separa por tipo.
 */
let _syncing = false;

function queueInOrder() {
  return QUEUE.slice().sort((a, b) =>
    String(a.event_datetime).localeCompare(String(b.event_datetime)));
}

function runsOf(items) {
  // Tramos consecutivos del mismo tipo, preservando el orden.
  const runs = [];
  for (const it of items) {
    const kind = it._kind === "cierre" ? "cierre" : "carga";
    if (runs.length && runs[runs.length - 1].kind === kind) {
      runs[runs.length - 1].items.push(it);
    } else {
      runs.push({ kind: kind, items: [it] });
    }
  }
  return runs;
}

async function dropFromQueue(uuid) {
  QUEUE = QUEUE.filter((q) => q.client_uuid !== uuid);
  await idbDel("queue", uuid);
}

async function syncQueue(manual) {
  if (_syncing) return;
  if (!QUEUE.length) {
    if (manual) { await refreshBootstrap(); toast("Nada pendiente por enviar", "ok"); }
    return;
  }
  if (!navigator.onLine) { if (manual) toast("Sin conexión", "err"); return; }

  _syncing = true;
  let enviados = 0, rechazados = 0, folioMsg = null;
  const warnings = [];
  try {
    for (const run of runsOf(queueInOrder())) {
      if (run.kind === "cierre") {
        for (const item of run.items) {
          const res = await fetch(API + "/drums/close", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              drum_number: item.drum_number,
              closed_at: item.log_date,
              sealed_by: item.sealed_by || null,
              seal_note: item.seal_note || null,
            }),
          });
          if (res.status === 404) {
            // El tambor ya no existe como abierto ni sellado: reintentar no va a
            // arreglarlo, así que sale de la cola con aviso en vez de quedar
            // trabando todo lo que viene detrás.
            await dropFromQueue(item.client_uuid);
            rechazados += 1;
            warnings.push("No se encontró el tambor N° " + item.drum_number + " para cerrar");
            continue;
          }
          if (!res.ok) throw new Error("HTTP " + res.status);
          // closed y already_closed: el servidor ya lo tiene sellado.
          await dropFromQueue(item.client_uuid);
          enviados += 1;
        }
        continue;
      }

      const res = await fetch(API + "/grinding-events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ events: run.items }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      for (const r of (data.results || [])) {
        // created y duplicate salen de la cola: en ambos casos el servidor ya lo
        // tiene. error se queda para reintentar o corregir.
        if (r.status === "created" || r.status === "duplicate") {
          await dropFromQueue(r.client_uuid);
          enviados += 1;
          if (r.folio_display) folioMsg = r.folio_display;
          (r.warnings || []).forEach((w) => warnings.push(w));
        } else if (r.error) {
          rechazados += 1;
          warnings.push(r.error);
        }
      }
    }

    await refreshBootstrap();
    renderHome();

    if (rechazados) {
      toast(rechazados + " rechazada(s): " + (warnings[0] || "revisa los datos"), "err");
    } else if (warnings.length) {
      toast(warnings[0], "warn");
    } else if (enviados) {
      toast(("Sincronizado" + (folioMsg ? " · folio " + folioMsg : "")), "ok");
    } else if (manual) {
      toast("Ya estaba sincronizado", "ok");
    }
  } catch (e) {
    renderHome();
    if (manual) toast("No se pudo sincronizar; queda en la cola", "err");
  } finally {
    _syncing = false;
  }
}

async function refreshBootstrap() {
  if (!navigator.onLine) return false;
  try {
    const res = await fetch(API + "/bootstrap");
    if (!res.ok) throw new Error("HTTP " + res.status);
    BOOT = await res.json();
    SYNCED = BOOT.recent_events || [];
    await idbPut("meta", BOOT, "bootstrap");
    return true;
  } catch (e) {
    return false;
  }
}

/* La ventana de seguridad (acceptGate / reopenGate) vive en un <script> dentro
 * de index.html, no acá: viene visible desde el HTML y debe poder aceptarse
 * aunque este archivo falle. Una pantalla de seguridad que depende de que
 * cargue el resto de la app deja al operador encerrado si algo se rompe. */

// --- arranque ---------------------------------------------------------------
async function init() {
  setNet();
  window.addEventListener("online", () => { setNet(); syncQueue(); });
  window.addEventListener("offline", setNet);

  await openDB();
  QUEUE = await idbAll("queue");
  const cached = await idbGet("meta", "bootstrap");
  if (cached) { BOOT = cached; SYNCED = cached.recent_events || []; }
  renderHome();

  if (await refreshBootstrap()) renderHome();
  if (QUEUE.length) syncQueue();

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
}
init();
