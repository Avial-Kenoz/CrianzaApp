"use strict";
/* Sexado offline — PWA. Checkout de un estanque, clasificación por PIT
   offline, y sincronización en dos vías. Reutiliza el patrón de /captura. */

const API = "/api/field/v1";
const SXAPI = "/api/field/v1/sexado";

// ---------------------------------------------------------------------------
// IndexedDB (best-effort: nunca rechaza)
// ---------------------------------------------------------------------------
let _db = null, _canPersist = false;
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("sexado", 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta");
      if (!db.objectStoreNames.contains("queue")) db.createObjectStore("queue", { keyPath: "client_uuid" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
function tx(s, m) { return _db.transaction(s, m).objectStore(s); }
function idbGet(s, k) { if (!_db) return Promise.resolve(undefined); return new Promise((r) => { try { const q = tx(s,"readonly").get(k); q.onsuccess=()=>r(q.result); q.onerror=()=>r(undefined); } catch(e){ r(undefined); } }); }
function idbPut(s, v, k) { if (!_db) return Promise.resolve(false); return new Promise((r) => { try { const q = tx(s,"readwrite").put(v,k); q.onsuccess=()=>r(true); q.onerror=()=>r(false); } catch(e){ r(false); } }); }
function idbDel(s, k) { if (!_db) return Promise.resolve(); return new Promise((r) => { try { const q = tx(s,"readwrite").delete(k); q.onsuccess=()=>r(); q.onerror=()=>r(); } catch(e){ r(); } }); }
function idbAll(s) { if (!_db) return Promise.resolve([]); return new Promise((r) => { try { const q = tx(s,"readonly").getAll(); q.onsuccess=()=>r(q.result||[]); q.onerror=()=>r([]); } catch(e){ r([]); } }); }

// ---------------------------------------------------------------------------
// Estado
// ---------------------------------------------------------------------------
const state = {
  session: null,       // {token, source, destinations, dev_state_rules}
  fishByPit: {},       // PIT -> fish dto
  queue: [],           // operaciones sin subir
  current: null,       // pez en clasificación
  form: { sex: null, dev: null },
  ponds: [],           // para el setup
};

const $ = (id) => document.getElementById(id);
function show(v) { ["view-setup","view-work","view-untagged"].forEach((x)=>$(x).classList.toggle("hidden", x!==v)); }
let _tt=null; function toast(m, k){ const t=$("toast"); t.textContent=m; t.className="toast show "+(k||""); clearTimeout(_tt); _tt=setTimeout(()=>t.classList.remove("show"),2600); }
function setNet(){ const on=navigator.onLine; $("net").className="net"+(on?"":" off"); $("net-txt").textContent=on?"en línea":"sin conexión"; }
function uuid(){ return crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random().toString(16).slice(2); }

// ---------------------------------------------------------------------------
// Setup / checkout
// ---------------------------------------------------------------------------
function _label(p) { return p.is_child ? `↳ ${p.parent_name} › ${p.name}` : p.name; }

async function loadPonds() {
  if (!navigator.onLine) { toast("Conéctate para iniciar una sesión", "err"); return; }
  try {
    const r = await fetch(SXAPI + "/ponds", { cache: "no-store" });
    const data = await r.json();
    state.ponds = data.ponds || [];   // ya ordenados: unidad → estanque → hijo
    renderSourceOptions();
    renderDests();
    $("src").onchange = renderDests;
  } catch (e) { toast("No se pudo cargar la lista de estanques", "err"); }
}
function renderSourceOptions() {
  let html = "", unit = null;
  for (const p of state.ponds) {
    if (p.locked) continue;                 // no se puede tomar un estanque ya en sesión
    if (p.unit_name !== unit) { if (unit !== null) html += "</optgroup>"; html += `<optgroup label="${p.unit_name}">`; unit = p.unit_name; }
    html += `<option value="${p.id}">${_label(p)}</option>`;
  }
  if (unit !== null) html += "</optgroup>";
  $("src").innerHTML = html || '<option value="">— sin estanques disponibles —</option>';
}
function renderDests() {
  const srcId = parseInt($("src").value, 10);
  let html = "", unit = null;
  for (const p of state.ponds) {
    if (p.id === srcId) continue;
    if (p.unit_name !== unit) { html += `<div class="dgroup">${p.unit_name}</div>`; unit = p.unit_name; }
    const dis = p.locked ? "disabled" : "";
    const note = p.locked ? ' <span class="muted">(en sesión)</span>' : "";
    html += `<label class="${p.is_child ? "child" : ""}"><input type="checkbox" class="dest" value="${p.id}" ${dis} /> ${_label(p)}${note}</label>`;
  }
  $("destlist").innerHTML = html;
}
async function doCheckout() {
  const srcId = parseInt($("src").value, 10);
  const dests = Array.from(document.querySelectorAll(".dest:checked")).map((c) => parseInt(c.value, 10));
  if (!srcId) { toast("Elige un estanque fuente", "err"); return; }
  if (dests.length > 10) { toast("Máximo 10 destinos", "err"); return; }
  try {
    const r = await fetch(SXAPI + "/checkout", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_pond_id: srcId, destination_pond_ids: dests }) });
    if (!r.ok) { const e = await r.json().catch(()=>({})); toast(e.error || ("Error " + r.status), "err"); return; }
    const snap = await r.json();
    state.session = { token: snap.token, source: snap.source, destinations: snap.destinations,
                      dev_state_rules: snap.dev_state_rules, untagged: snap.untagged_balances || [],
                      fishCount: (snap.fish||[]).length };
    state.fishByPit = {};
    (snap.fish || []).forEach((f) => { if (f.pit) state.fishByPit[f.pit.toUpperCase()] = f; });
    await idbPut("meta", state.session, "session");
    await idbPut("meta", state.fishByPit, "fishByPit");
    toast(`Sesión iniciada: ${snap.source.name} (${state.session.fishCount} peces)`, "ok");
    enterWork();
  } catch (e) { toast("No se pudo iniciar la sesión", "err"); }
}

// ---------------------------------------------------------------------------
// Trabajo
// ---------------------------------------------------------------------------
function enterWork() {
  show("view-work");
  $("sess-badge").textContent = state.session ? ("Estanque: " + state.session.source.name) : "Sin sesión";
  $("pending-n").textContent = state.queue.length;
  $("pending-badge").className = "badge" + (state.queue.length ? " warn" : "");
  $("work-hint").textContent = `${state.session.fishCount} peces en la foto. Busca o escanea un PIT.`;
  renderRecent(); updateActionLabel();
  setTimeout(() => $("pit-search").focus(), 100);
}
function renderRecent() {
  const last = state.queue.slice(-6).reverse();
  const destName = (id) => ((state.session.destinations || []).find((x) => x.id === id) || {}).name || id;
  $("recent").innerHTML = last.length ? ('<div class="list-title">Últimos</div>' + last.map((o) => {
    if (o.kind === "move_untagged")
      return `<div class="r"><span class="p">sin marca</span><span class="done">Lote ${o.lot_id}: ${o.quantity} → ${destName(o.move_to)} ✓</span></div>`;
    const cls = [o.sex ? (o.sex === "f" ? "H" : "M") : null, o.development_state, o.move_to ? ("→ " + destName(o.move_to)) : null]
      .filter(Boolean).join(" · ");
    return `<div class="r"><span class="p">${o.pit}</span><span class="done">${cls || "guardado"} ✓</span></div>`;
  }).join("")) : "";
}
function findPit(raw) {
  const pit = (raw || "").trim().toUpperCase();
  if (!pit) return;
  const fish = state.fishByPit[pit];
  if (fish) { openFish(fish); return; }
  // PIT no está en la foto
  const lots = (state.session.untagged || []).filter((b) => b.quantity > 0);
  if (lots.length) { openRegister(pit); return; }
  if (confirm("PIT no está en el estanque y no hay peces sin marca para registrar. ¿Enviarlo a reconciliación (tag ajeno / retag)?")) {
    const op = { client_uuid: uuid(), kind: "foreign_tag", pit: pit, captured_at: new Date().toISOString() };
    state.queue.push(op); idbPut("queue", op);
    $("pending-n").textContent = state.queue.length; $("pending-badge").className = "badge warn";
    $("pit-search").value = ""; renderRecent(); updateActionLabel();
    toast("Enviado a reconciliación", "ok");
  }
}
function openRegister(pit) {
  state.registerMode = true;
  state.current = { pit: pit, id: null };
  state.form = { sex: null, dev: null };
  $("f-pit").textContent = pit;
  $("f-cur").textContent = "🆕 PIT nuevo — registrar desde saldo sin marca";
  const lots = (state.session.untagged || []).filter((b) => b.quantity > 0);
  $("f-reglot").innerHTML = lots.map((b) => `<option value="${b.lot_id}">Lote ${b.lot_label} (${b.quantity} disp.)</option>`).join("");
  $("field-reglot").classList.remove("hidden");
  $("f-weight").value = ""; $("f-diam").value = "";
  const dests = state.session.destinations || [];
  $("f-move").innerHTML = '<option value="">— No mover —</option>' +
    dests.map((d) => `<option value="${d.id}">${d.name}${d.depuration ? " (depuración)" : ""}</option>`).join("");
  $("fish-card").classList.remove("hidden");
  renderSexAndDev(); validate();
  $("fish-card").scrollIntoView({ behavior: "smooth", block: "start" });
}
function openFish(fish) {
  state.registerMode = false;
  $("field-reglot").classList.add("hidden");
  state.current = fish;
  state.form = { sex: fish.sex || null, dev: fish.development_state || null };
  $("f-pit").textContent = fish.pit;
  $("f-cur").textContent = `Actual: ${fish.sex? (fish.sex==="f"?"hembra":"macho"):"sin sexar"}` +
    (fish.development_state? ` · estado ${fish.development_state}`:"") + (fish.weight? ` · ${fish.weight} g`:"");
  $("f-weight").value = ""; $("f-diam").value = "";
  const dests = state.session.destinations || [];
  $("f-move").innerHTML = '<option value="">— No mover —</option>' +
    dests.map((d) => `<option value="${d.id}">${d.name}${d.depuration ? " (depuración)" : ""}</option>`).join("");
  $("fish-card").classList.remove("hidden");
  renderSexAndDev(); validate();
  $("fish-card").scrollIntoView({ behavior: "smooth", block: "start" });
}
function renderSexAndDev() {
  $("seg-f").className = "seg" + (state.form.sex==="f"?" on-f":"");
  $("seg-m").className = "seg" + (state.form.sex==="m"?" on-m":"");
  $("field-diam").classList.toggle("hidden", state.form.sex!=="f");
  const rules = (state.session.dev_state_rules || {})[state.form.sex] || [];
  $("devstate").innerHTML = rules.map((d) =>
    `<button type="button" data-dev="${d}" class="${state.form.dev===d?"on":""}">${d}</button>`).join("") ||
    '<span class="muted">Elige el sexo primero</span>';
  $("devstate").querySelectorAll("button").forEach((b) => b.onclick = () => { state.form.dev = b.dataset.dev; renderSexAndDev(); validate(); });
}
function validate() {
  const f = state.form; let err = "";
  if (f.dev && !f.sex) err = "Indica el sexo para fijar el estado.";
  const rules = (state.session.dev_state_rules || {})[f.sex] || [];
  if (f.dev && f.sex && !rules.includes(f.dev)) err = "Estado no válido para ese sexo.";
  $("valerr").textContent = err; $("valerr").classList.toggle("hidden", !err);
  $("btn-save").disabled = !!err;
  return !err;
}
function setSex(sex) { state.form.sex = sex; if (state.form.dev && !((state.session.dev_state_rules||{})[sex]||[]).includes(state.form.dev)) state.form.dev = null; renderSexAndDev(); validate(); }
async function saveClassification() {
  if (!validate()) return;
  const f = state.form, fish = state.current, reg = state.registerMode;
  const w = parseFloat($("f-weight").value.replace(",", "."));
  const d = parseFloat($("f-diam").value.replace(",", "."));
  const moveTo = $("f-move").value ? parseInt($("f-move").value, 10) : null;
  const pitU = fish.pit.toUpperCase();
  let op;
  if (reg) {
    const lotId = $("f-reglot").value ? parseInt($("f-reglot").value, 10) : null;
    if (!lotId) { toast("Elige el lote", "err"); return; }
    op = { client_uuid: uuid(), kind: "register", pit: fish.pit, lot_id: lotId,
           sex: f.sex, weight: isFinite(w) ? w : null, diameter: (f.sex === "f" && isFinite(d)) ? d : null,
           development_state: f.dev, move_to: moveTo, captured_at: new Date().toISOString() };
    const b = (state.session.untagged || []).find((x) => x.lot_id === lotId);
    if (b) b.quantity -= 1;
    await idbPut("meta", state.session, "session");
    if (!moveTo) {  // el pez nuevo queda en la foto local
      state.fishByPit[pitU] = { id: null, pit: fish.pit, sex: f.sex, development_state: f.dev, weight: isFinite(w) ? w : null, state: "alive" };
      await idbPut("meta", state.fishByPit, "fishByPit");
    }
  } else {
    if (!f.sex && !isFinite(w) && !f.dev && !moveTo) { toast("Nada que guardar", "err"); return; }
    op = { client_uuid: uuid(), kind: "fish_save", pit: fish.pit, fish_id: fish.id,
           sex: f.sex, weight: isFinite(w) ? w : null, diameter: (f.sex === "f" && isFinite(d)) ? d : null,
           development_state: f.dev, move_to: moveTo, captured_at: new Date().toISOString() };
    fish.sex = f.sex; fish.development_state = f.dev; if (isFinite(w)) fish.weight = w;
    if (moveTo) { delete state.fishByPit[pitU]; await idbPut("meta", state.fishByPit, "fishByPit"); }
  }
  state.queue.push(op);
  await idbPut("queue", op);
  state.registerMode = false; $("field-reglot").classList.add("hidden");
  $("fish-card").classList.add("hidden");
  $("pit-search").value = "";
  $("pending-n").textContent = state.queue.length;
  $("pending-badge").className = "badge warn";
  renderRecent(); updateActionLabel();
  toast((reg ? "PIT nuevo registrado" : "Clasificación guardada") + (_canPersist ? "" : " (en memoria)"), "ok");
  $("pit-search").focus();
}

// ---------------------------------------------------------------------------
// Peces sin marca (movimiento agregado por lote)
// ---------------------------------------------------------------------------
function openUntagged() { renderUntagged(); show("view-untagged"); }
function renderUntagged() {
  const dests = state.session.destinations || [];
  const lots = (state.session.untagged || []).filter((b) => b.quantity > 0);
  if (!lots.length) { $("untagged-list").innerHTML = '<p class="muted">No hay peces sin marca en este estanque.</p>'; return; }
  const opts = '<option value="">— destino —</option>' + dests.map((d) => `<option value="${d.id}">${d.name}</option>`).join("");
  $("untagged-list").innerHTML = lots.map((b) => `
    <div class="fish" style="margin-bottom:10px">
      <div><b>Lote ${b.lot_label}</b> · <span>${b.quantity}</span> disponibles</div>
      <div class="field mt"><label>Cantidad a mover</label><input class="num" type="number" inputmode="numeric" min="1" max="${b.quantity}" id="uqty-${b.lot_id}" placeholder="0" /></div>
      <div class="field"><label>Destino</label><select id="udest-${b.lot_id}">${opts}</select></div>
      <button class="btn btn-primary" onclick="window._moveUntagged(${b.lot_id})">Mover</button>
    </div>`).join("");
}
async function moveUntagged(lotId) {
  const b = (state.session.untagged || []).find((x) => x.lot_id === lotId);
  if (!b) return;
  const qty = parseInt(($("uqty-"+lotId)||{}).value, 10);
  const dest = parseInt(($("udest-"+lotId)||{}).value, 10);
  if (!qty || qty <= 0) { toast("Ingresa la cantidad", "err"); return; }
  if (qty > b.quantity) { toast("No hay tantos disponibles", "err"); return; }
  if (!dest) { toast("Elige el destino", "err"); return; }
  const op = { client_uuid: uuid(), kind: "move_untagged", lot_id: lotId, quantity: qty,
               move_to: dest, captured_at: new Date().toISOString() };
  state.queue.push(op); await idbPut("queue", op);
  b.quantity -= qty; await idbPut("meta", state.session, "session");
  $("pending-n").textContent = state.queue.length; $("pending-badge").className = "badge warn";
  renderUntagged(); renderRecent();
  toast(`Movidos ${qty} sin marca` + (_canPersist?"":" (en memoria)"), "ok");
}
window._moveUntagged = moveUntagged;

// Etiqueta contextual del botón de acción: Buscar (existe) / Registrar (nuevo)
function updateActionLabel() {
  const btn = $("btn-action"); if (!btn) return;
  const pit = ($("pit-search").value || "").trim().toUpperCase();
  if (pit && !state.fishByPit[pit]) {
    const lots = ((state.session && state.session.untagged) || []).filter((b) => b.quantity > 0);
    btn.textContent = lots.length ? "Registrar" : "Buscar";
  } else {
    btn.textContent = "Buscar";
  }
}

// ---------------------------------------------------------------------------
// Sync / salir
// ---------------------------------------------------------------------------
let _syncing=false;
async function syncQueue() {
  if (_syncing) return;
  if (!state.session) { toast("No hay sesión", "err"); return; }
  if (!navigator.onLine) { toast("Sin conexión para sincronizar", "err"); return; }
  if (state.queue.length === 0) { toast("Nada por sincronizar"); return; }
  _syncing = true;
  try {
    const r = await fetch(SXAPI + "/sync", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: state.session.token, operations: state.queue }) });
    if (!r.ok) { const e=await r.json().catch(()=>({})); toast(e.error || ("Error "+r.status), "err"); return; }
    const res = await r.json();
    // sacar de la cola todo lo que quedó resuelto (aplicado/duplicado/en bandeja)
    const settled = new Set(res.results.filter(x => ["applied","duplicate","pending_review"].includes(x.status)).map(x => x.client_uuid));
    for (const uid of settled) await idbDel("queue", uid);
    state.queue = state.queue.filter(o => !settled.has(o.client_uuid));
    $("pending-n").textContent = state.queue.length;
    $("pending-badge").className = "badge" + (state.queue.length?" warn":"");
    renderRecent();
    const c = res.counts;
    let msg = `Sincronizado: ${c.applied} aplicadas`;
    if (c.pending_review) msg += `, ${c.pending_review} a reconciliar`;
    toast(msg, c.pending_review?"":"ok");
    if (res.session_status === "synced") {
      toast("Sesión cerrada. Estanque liberado.", "ok");
      await clearSession();
    }
  } catch(e){ toast("Sincronización pendiente: " + e.message, "err"); }
  finally { _syncing=false; }
}
async function clearSession() {
  state.session=null; state.fishByPit={}; state.queue=[]; state.current=null;
  await idbDel("meta","session"); await idbDel("meta","fishByPit");
  for (const o of await idbAll("queue")) await idbDel("queue", o.client_uuid);
  $("sess-badge").textContent="Sin sesión";
  await loadPonds(); show("view-setup");
}
async function exitSession() {
  if (state.queue.length && !confirm("Hay lecturas sin sincronizar. ¿Salir de todos modos? (se perderán si no sincronizas)")) return;
  // libera el bloqueo en el servidor si hay conexión
  if (navigator.onLine && state.session) {
    try { await fetch(SXAPI + "/release", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ token: state.session.token }) }); } catch(e){}
  }
  await clearSession();
  toast("Sesión finalizada");
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
function wire() {
  $("btn-checkout").addEventListener("click", doCheckout);
  $("btn-action").addEventListener("click", () => findPit($("pit-search").value));
  $("pit-search").addEventListener("input", updateActionLabel);
  $("btn-untagged").addEventListener("click", openUntagged);
  $("btn-untagged-back").addEventListener("click", () => show("view-work"));
  $("btn-sync").addEventListener("click", syncQueue);
  $("btn-exit").addEventListener("click", exitSession);
  $("btn-cancel").addEventListener("click", () => { state.registerMode=false; $("field-reglot").classList.add("hidden"); $("fish-card").classList.add("hidden"); $("pit-search").value=""; updateActionLabel(); $("pit-search").focus(); });
  $("btn-save").addEventListener("click", saveClassification);
  $("seg-f").addEventListener("click", () => setSex("f"));
  $("seg-m").addEventListener("click", () => setSex("m"));
  $("pit-search").addEventListener("keydown", (e) => { if (e.key === "Enter") findPit($("pit-search").value); });
  window.addEventListener("online", () => { setNet(); });
  window.addEventListener("offline", setNet);
}
async function init() {
  window.addEventListener("error", (e) => toast("Error: " + (e.message || "script"), "err"));
  window.addEventListener("unhandledrejection", (e) => toast("Error: " + ((e.reason&&e.reason.message)||e.reason||"async"), "err"));
  wire(); setNet();
  try {
    _db = await openDB();
    state.queue = await idbAll("queue");
    state.session = await idbGet("meta", "session");
    state.fishByPit = (await idbGet("meta", "fishByPit")) || {};
  } catch(e) { _db=null; toast("Sin almacenamiento local: " + (e.message||e), "err"); }
  _canPersist = (await idbPut("meta", { t: Date.now() }, "__probe__")) === true;
  const np=$("nopersist"); if (np) np.classList.toggle("hidden", _canPersist);

  if (state.session && state.session.token) { enterWork(); }
  else { await loadPonds(); show("view-setup"); }

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(()=>{});
}
init();
