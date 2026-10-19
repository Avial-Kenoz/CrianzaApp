"use strict";
/* Módulo Dosificación de biofiltros (caliza, bicarbonato, sal).

   Muestra SOLO lo que instruyó el supervisor desde la vista de la unidad
   («Enviar a tablet»). La sugerencia del modelo nunca llega acá: el bootstrap
   de /api/dosing/v1 no la trae.

   Una fila por (unidad, producto), para que las 5 unidades quepan en una
   pantalla. El campo trae LO QUE FALTA del día (no el total): así se reparte
   en varias veces sin hacer cuentas, y confirmar dos veces sin mirar no
   duplica la dosis. «Confirmar» registra de un toque.

   Deshacer: el registro queda retenido UNDO_MS antes de entrar a la cola, así
   deshacer es simplemente no enviarlo (nada que anular en el servidor). Si la
   app se cierra en esa ventana, se envía al volver a abrir: estaba confirmado.

   «Hoy» = lo aplicado según el servidor al descargar + lo registrado en este
   equipo que el servidor aún no contaba (retenido, pendiente, o subido después
   de la última descarga). Así no se cuenta dos veces ni se pierde lo offline.

   Pasarse de lo instruido se permite con confirmación, sin bloquear: si se
   bloqueara, el operador terminaría digitando mal la cantidad para que pase. */

(() => {
  const API = "/api/dosing/v1";
  const STALE_MIN = 30;
  const MAX_KG = 9999;
  const UNDO_MS = 5000;
  let C = null;          // contexto del núcleo
  let root = null;
  let boot = null;       // último bootstrap
  let bootAt = 0;        // ms en que se PIDIÓ ese bootstrap
  // Registros locales: {client_uuid, unit_id, product, kg, day, held, payload, synced, syncedAt}
  let log = [];
  let seen = new Set();  // instrucciones ya vistas (para marcar «Nueva»)
  const drafts = {};     // ins.id -> texto del campo mientras se edita
  const notes = {};      // ins.id -> nota abierta (string) ; ausente = cerrada
  const timers = {};     // client_uuid -> timeout de retención

  async function init(ctx, el) {
    C = ctx; root = el;
    root.addEventListener("click", onClick);
    root.addEventListener("input", onInput);
    root.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.matches(".dz-in")) confirmRow(Number(e.target.dataset.ins));
    });
    document.getElementById("undo-btn").addEventListener("click", undoLast);
    boot = (await C.idbGet("meta", "dosing.boot")) || null;
    bootAt = (await C.idbGet("meta", "dosing.bootAt")) || 0;
    log = (await C.idbGet("meta", "dosing.log")) || [];
    seen = new Set((await C.idbGet("meta", "dosing.seen")) || []);
    pruneLog();
    // Retenidos de una sesión anterior (se cerró la app antes de los 5 s).
    for (const e of log.filter((x) => x.held)) await release(e.client_uuid);
    render();
  }

  // --- datos ----------------------------------------------------------------
  async function refresh() {
    const askedAt = Date.now();
    const b = await C.fetchJSON(API + "/bootstrap");
    boot = b; bootAt = askedAt;
    C.idbPut("meta", boot, "dosing.boot");
    C.idbPut("meta", bootAt, "dosing.bootAt");
    // La primera descarga de un equipo nuevo no marca todo como «Nueva».
    if (!seen.size && !log.length) markSeen(b.instructions.map((i) => i.id));
    render();
  }

  async function flush(payloads) {
    const r = await C.fetchJSON(API + "/doses", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doses: payloads }),
    }, 15000);
    const done = [], errors = {};
    for (const x of r.results || []) {
      if (x.status === "created" || x.status === "duplicate") done.push(x.client_uuid);
      else errors[x.client_uuid] = x.error || "error";
    }
    const now = Date.now();
    for (const e of log) if (done.includes(e.client_uuid)) { e.synced = true; e.syncedAt = now; }
    saveLog();
    render();
    return { done, errors };
  }

  function pruneLog() {
    const cut = C.localISO(new Date(Date.now() - 3 * 86400000)).slice(0, 10);
    log = log.filter((e) => e.day >= cut || !e.synced);
  }
  function saveLog() { pruneLog(); C.idbPut("meta", log, "dosing.log"); }
  function markSeen(ids) {
    ids.forEach((i) => seen.add(i));
    C.idbPut("meta", Array.from(seen).slice(-300), "dosing.seen");
  }

  function appliedToday(ins) {
    const today = C.todayStr();
    const fresh = boot && boot.today === today;
    // Si el bootstrap es de otro día, lo del servidor no cuenta para hoy.
    const server = fresh ? (ins.applied_today_kg || 0) : 0;
    const local = log
      .filter((e) => e.day === today && e.unit_id === ins.unit_id && e.product === ins.product)
      .filter((e) => !e.synced || e.syncedAt > bootAt || !fresh)
      .reduce((s, e) => s + e.kg, 0);
    return server + local;
  }
  function remaining(ins) { return Math.max(0, ins.kg_day - appliedToday(ins)); }

  // --- vista ----------------------------------------------------------------
  function shortLabel(ins) {
    return { "carbonato de calcio": "Caliza", "bicarbonato": "Bicarbonato", "sal": "Sal" }[ins.product]
      || ins.product_label;
  }

  function render() {
    // No pisar lo que el operador está escribiendo (el refresco es cada minuto).
    const act = document.activeElement;
    const focusIns = act && root.contains(act) && act.dataset ? act.dataset.ins : null;
    const focusCls = act && act.classList ? (act.classList.contains("dz-in") ? "dz-in" : (act.closest(".dz-note") ? "note" : null)) : null;
    const { esc, fmtKg, fmtWhen } = C;

    if (!boot) {
      root.innerHTML = `<div class="empty">Aún no hay instrucciones descargadas.<br>Conéctate a la red de la base; se descargan solas.</div>`;
      return;
    }
    const list = boot.instructions || [];
    let html = "";
    if (!list.length) {
      html += `<div class="empty">No hay instrucciones de dosificación vigentes.</div>`;
    } else {
      html += `<div class="dz-head"><span>Unidad</span><span>Producto</span><span style="text-align:right">Instruido</span>` +
              `<span>Hoy</span><span style="text-align:center">Aplicar kg</span><span></span><span></span></div>`;
    }
    let lastUnit = null;
    for (const ins of list) {
      const first = ins.unit_name !== lastUnit;
      lastUnit = ins.unit_name;
      const done = appliedToday(ins);
      const target = ins.kg_day;
      const over = done > target + 0.05;
      const complete = target > 0 && !over && done >= target - 0.05;
      const pct = target > 0 ? Math.min(100, (done / target) * 100) : (done > 0 ? 100 : 0);
      const falta = remaining(ins);
      const val = ins.id in drafts ? drafts[ins.id] : (falta > 0.05 ? fmtKg(falta) : "");
      const isNew = !seen.has(ins.id);
      const noteOpen = ins.id in notes;
      html += `
        <div class="dz-row ${first ? "first" : ""} ${isNew ? "is-new" : ""}" data-row="${ins.id}">
          <div class="dz-unit">${first ? esc(ins.unit_name) : ""}</div>
          <div class="dz-prod">${esc(shortLabel(ins))}${isNew ? `<span class="nw">NUEVA</span>` : ""}
            ${ins.note ? `<span class="nt">${esc(ins.note)}</span>` : ""}</div>
          <div class="dz-ins">${target > 0 ? fmtKg(target) + " kg" : "No aplicar"}</div>
          <div class="dz-today ${over ? "over" : ""}">
            <b>${fmtKg(done)}</b>${complete ? ` <span class="ok">✓</span>` : ""}${over ? ` <span class="ov">+${fmtKg(done - target)}</span>` : ""}
            <div class="bar"><i style="width:${pct}%"></i></div>
          </div>
          <input class="dz-in" data-ins="${ins.id}" type="text" inputmode="decimal" autocomplete="off"
                 value="${esc(val)}" placeholder="—" aria-label="kg a aplicar" />
          <button class="dz-nb ${noteOpen ? "on" : ""}" data-note="${ins.id}" title="Agregar observación" aria-label="Observación">✎</button>
          <button class="dz-ok" data-ok="${ins.id}">Confirmar</button>
          ${noteOpen ? `<div class="dz-note"><input data-ins="${ins.id}" maxlength="255" placeholder="Observación (opcional)" value="${esc(notes[ins.id])}" /></div>` : ""}
        </div>`;
    }
    const old = (Date.now() - bootAt) / 60000 > STALE_MIN;
    html += `<div class="stale ${old ? "old" : ""}">Instrucciones al ${fmtWhen(new Date(bootAt).toISOString())}` +
            `${old ? " · sin conexión reciente: pueden haber cambiado" : ""}</div>`;
    const pend = C.pendingFor("dosificacion").length;
    if (pend) html += `<div class="stale">${pend} registro(s) por subir</div>`;
    root.innerHTML = html;

    if (focusIns && focusCls) {
      const sel = focusCls === "dz-in" ? `.dz-in[data-ins="${focusIns}"]` : `.dz-note input[data-ins="${focusIns}"]`;
      const el = root.querySelector(sel);
      if (el) { el.focus(); const n = el.value.length; try { el.setSelectionRange(n, n); } catch (e) { /* type sin selección */ } }
    }
  }

  // --- registrar ------------------------------------------------------------
  async function confirmRow(insId) {
    const ins = (boot.instructions || []).find((i) => i.id === insId);
    if (!ins) return;
    const input = root.querySelector(`.dz-in[data-ins="${insId}"]`);
    const kg = C.parseNum(input ? input.value : "");
    if (!isFinite(kg) || kg <= 0) { C.toast("Escribe los kg aplicados", "err"); if (input) input.focus(); return; }
    if (kg > MAX_KG) { C.toast("Cantidad fuera de rango", "err"); return; }

    const done = appliedToday(ins);
    if (ins.kg_day <= 0) {
      if (!(await C.confirm("La instrucción es no aplicar",
        `Para ${ins.product_label} en ${ins.unit_name} se instruyó 0 kg. ¿Registrar ${C.fmtKg(kg)} kg igual?`))) return;
    } else if (done + kg > ins.kg_day + 0.05) {
      if (!(await C.confirm("Vas a pasar lo instruido",
        `${ins.unit_name}: con esto llegas a ${C.fmtKg(done + kg)} kg de ${shortLabel(ins).toLowerCase()} hoy; ` +
        `lo instruido es ${C.fmtKg(ins.kg_day)} kg. ¿Registrar igual?`))) return;
    }

    const now = new Date();
    const obs = ((notes[insId] || "") + "").trim();
    const payload = {
      client_uuid: C.uuid(),
      instruction_id: ins.id,
      applied_at: C.localISO(now),
      kg: Math.round(kg * 100) / 100,
      observation: obs || null,
    };
    log.push({ client_uuid: payload.client_uuid, unit_id: ins.unit_id, product: ins.product,
               kg: payload.kg, day: C.todayStr(), held: true, payload, synced: false, syncedAt: 0 });
    saveLog();
    delete drafts[insId];
    delete notes[insId];
    markSeen([insId]);
    if (document.activeElement) document.activeElement.blur();
    render();

    showUndo(`${ins.unit_name} · ${C.fmtKg(payload.kg)} kg de ${shortLabel(ins).toLowerCase()}`, payload.client_uuid);
    timers[payload.client_uuid] = setTimeout(() => release(payload.client_uuid), UNDO_MS);
  }

  // Pasa un registro retenido a la cola común (de ahí sube cuando haya red).
  async function release(uuid) {
    clearTimeout(timers[uuid]); delete timers[uuid];
    const e = log.find((x) => x.client_uuid === uuid);
    if (!e || !e.held) return;
    e.held = false;
    const payload = e.payload; delete e.payload;
    saveLog();
    hideUndo(uuid);
    await C.enqueue("dosificacion", payload);
    render();
  }

  let undoUuid = null;
  let undoHide = null;
  function showUndo(text, uuid) {
    undoUuid = uuid;
    document.getElementById("undo-txt").textContent = "Guardado: " + text;
    document.getElementById("undo").classList.add("show");
    clearTimeout(undoHide);
    undoHide = setTimeout(() => hideUndo(uuid), UNDO_MS);
  }
  function hideUndo(uuid) {
    if (uuid && uuid !== undoUuid) return;
    undoUuid = null;
    document.getElementById("undo").classList.remove("show");
  }
  function undoLast() {
    const uuid = undoUuid;
    const e = log.find((x) => x.client_uuid === uuid);
    hideUndo();
    if (!e || !e.held) { C.toast("Ya se registró; no se puede deshacer", "err"); return; }
    clearTimeout(timers[uuid]); delete timers[uuid];
    log = log.filter((x) => x.client_uuid !== uuid);
    saveLog();
    render();
    C.toast("Deshecho: no se registró", "ok");
  }

  // --- eventos --------------------------------------------------------------
  function onInput(e) {
    const id = Number(e.target.dataset.ins);
    if (!id) return;
    if (e.target.classList.contains("dz-in")) drafts[id] = e.target.value;
    else if (e.target.closest(".dz-note")) notes[id] = e.target.value;
  }
  function onClick(e) {
    const ok = e.target.closest("[data-ok]");
    if (ok) { confirmRow(Number(ok.dataset.ok)); return; }
    const nb = e.target.closest("[data-note]");
    if (nb) {
      const id = Number(nb.dataset.note);
      if (id in notes) delete notes[id]; else notes[id] = "";
      render();
      const el = root.querySelector(`.dz-note input[data-ins="${id}"]`);
      if (el) el.focus();
      return;
    }
    const row = e.target.closest("[data-row]");
    if (row && !seen.has(Number(row.dataset.row))) { markSeen([Number(row.dataset.row)]); render(); }
  }

  Terreno.register({
    id: "dosificacion",
    label: "Dosificación",
    init, refresh, flush,
    onShow: render,
  });
})();
