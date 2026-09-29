"""UI web del encargado de mantenimiento (PR1: catálogo y configuración).

Convención de CrianzaApp: páginas bajo /views/ui/* renderizadas con jinja_env +
HTMLResponse e incluyendo _sidebar.html. Sin capa de auth: quien registra se
anota en «Registrado por» y no queda acreditado (spec §9.4).

Los handlers son delgados: parsean el formulario y llaman a service.py, que es
donde viven las reglas (las mismas que usarán la API de Planta y el bot).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import segno
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader

from app.db.session import SessionLocal
from app.maintenance import rules, service
from app.maintenance.models import (
    SITIOS, SITIO_LABELS, MntAviso, MntContratista, MntEquipo, MntOt, MntPersona,
    MntSistema, MntTipoEquipo, MntParametro,
)
from app.maintenance.service import ErrorValidacion

PREFIX = "/views/ui/mantenimiento"
router = APIRouter(prefix=PREFIX, tags=["mantenimiento"])
template_dir = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)))


def _render(nombre: str, request: Request, **ctx) -> HTMLResponse:
    base = {
        "request": request,
        "prefix": PREFIX,
        "sitio_labels": SITIO_LABELS,
        "criticidad_labels": rules.CRITICIDAD_LABELS,
        "estados_equipo": service.ESTADOS_EQUIPO,
        "roles_persona": service.ROLES_PERSONA,
        "msg": request.query_params.get("msg"),
        "err": request.query_params.get("err"),
    }
    base.update(ctx)
    return HTMLResponse(jinja_env.get_template(nombre).render(base))


def _volver(url: str, msg: Optional[str] = None, err: Optional[str] = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote_plus(msg)}"
    elif err:
        url += f"{sep}err={quote_plus(err)}"
    return RedirectResponse(url=url, status_code=303)


def _encuesta_ctx() -> dict:
    return {
        "preg_impacto": rules.PREGUNTA_IMPACTO,
        "opc_impacto": rules.OPCIONES_IMPACTO,
        "impacto_txt": {s: dict(o) for s, o in rules.OPCIONES_IMPACTO.items()},
        "preg_respaldo": rules.PREGUNTA_RESPALDO,
        "opc_respaldo": rules.OPCIONES_RESPALDO,
        "preg_reposicion": rules.PREGUNTA_REPOSICION,
        "opc_reposicion": rules.OPCIONES_REPOSICION,
        "preg_seguridad": rules.PREGUNTA_SEGURIDAD,
        "opc_seguridad": rules.OPCIONES_SEGURIDAD,
    }


def _ficha_ctx(db, sitio: Optional[str] = None, excluir_id: Optional[int] = None) -> dict:
    """Listas para los selects de la ficha de equipo."""
    candidatos = [e for e in service.equipos(db) if e.id != excluir_id]
    # Se pasan también los inactivos: la plantilla los muestra solo si el equipo
    # ya los tiene. Si no, editar la ficha de un equipo cuyo sistema se
    # desactivó le borraría el sistema en silencio.
    return {
        "sistemas": service.sistemas(db, solo_activos=False),
        "tipos": service.tipos(db, solo_activos=False),
        "contratistas": service.contratistas(db, solo_activos=False),
        "respaldos": candidatos,
        "destinos_disp": service.destinos_disponibles(db),
        "catalogo_tecnico": service.catalogo_tecnico(db),
        "nombres_personas": [p.nombre for p in service.personas(db)],
        **_encuesta_ctx(),
    }


def _bot_username() -> str:
    return (os.getenv("MNT_TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
def home():
    return RedirectResponse(url=f"{PREFIX}/tablero", status_code=303)


def _parse_dt(valor: Optional[str]) -> Optional[datetime]:
    """'2026-09-29T14:30' (datetime-local) → datetime; vacío → None."""
    v = (valor or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise ErrorValidacion(f"Hora inválida: {v}")


def _ot_ctx() -> dict:
    return {
        "estados_ot": rules.ESTADOS_OT, "estados_abiertos": rules.ESTADOS_ABIERTOS,
        "estados_iniciales": service.ESTADOS_INICIALES, "tramos_labels": rules.TRAMOS,
        "prioridades": rules.PRIORIDADES, "condiciones": rules.CONDICIONES,
        "respaldo_entro": rules.RESPALDO_ENTRO, "folio_ot": rules.folio_ot,
        "folio_aviso": rules.folio_aviso, "dur": rules.formato_duracion,
    }


def _fila_ot(ot, equipo, eventos, ahora) -> dict:
    t = service.tramos_de(ot, eventos, ahora)
    return {
        "ot": ot, "equipo": equipo, "tramos": t,
        "vencida": service.ot_fuera_de_plazo(ot, ahora),
        "edad": ((ot.cierre_at or ahora) - ot.inicio_at).total_seconds() / 3600.0,
        "restante": (float(ot.plazo_horas) - ((ahora - ot.inicio_at).total_seconds() / 3600.0))
                    if ot.plazo_horas is not None and ot.estado in rules.ESTADOS_ABIERTOS else None,
    }


# ---------------------------------------------------------------------------
# Tablero
# ---------------------------------------------------------------------------
@router.get("/tablero", response_class=HTMLResponse)
def tablero(request: Request, sitio: str = ""):
    db = SessionLocal()
    try:
        sitio = sitio if sitio in SITIOS else ""
        ahora = datetime.now()
        equipos = {e.id: e for e in service.equipos(db, incluir_baja=True)}
        avisos = service.avisos_nuevos(db, sitio or None)
        abiertas = service.ots(db, sitio=sitio or None)
        cerradas = service.ots(db, sitio=sitio or None, abiertas=False, desde=ahora - timedelta(days=14))
        ev = service.eventos_por_ot(db, [o.id for o in abiertas + cerradas])
        filas = [_fila_ot(o, equipos.get(o.equipo_id), ev[o.id], ahora) for o in abiertas]
        filas_cerr = [_fila_ot(o, equipos.get(o.equipo_id), ev[o.id], ahora) for o in cerradas]
        # Avisos nuevos cuyo equipo ya tiene OT abierta: se ofrecen para unir.
        abierta_por_equipo = {o.equipo_id: o for o in abiertas}
        return _render(
            "mantenimiento_tablero.html", request, active="tablero", f_sitio=sitio,
            avisos=[{"a": a, "equipo": equipos.get(a.equipo_id), "ot_abierta": abierta_por_equipo.get(a.equipo_id),
                     "espera": (ahora - a.detectado_at).total_seconds() / 3600.0} for a in avisos],
            filas=filas, filas_cerradas=filas_cerr, ahora=ahora,
            kpi={"avisos": len(avisos), "abiertas": len(abiertas),
                 "p1": sum(1 for o in abiertas if o.prioridad == "P1"),
                 "vencidas": sum(1 for f in filas if f["vencida"])},
            nombres_personas=[p.nombre for p in service.personas(db)],
            **_ot_ctx(),
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Avisos
# ---------------------------------------------------------------------------
@router.get("/avisos/nuevo", response_class=HTMLResponse)
def aviso_nuevo_form(request: Request, equipo_id: Optional[int] = None):
    db = SessionLocal()
    try:
        return _render(
            "mantenimiento_aviso_nuevo.html", request, active="tablero",
            equipos=service.equipos(db), equipo_sel=equipo_id,
            respaldos={e.id: e.respaldo_equipo_id for e in service.equipos(db)},
            ahora=datetime.now(), nombres_personas=[p.nombre for p in service.personas(db)],
            **_ot_ctx(),
        )
    finally:
        db.close()


@router.post("/avisos/nuevo")
async def aviso_nuevo(request: Request):
    form = await request.form()
    foto = form.get("foto")
    contenido = await foto.read() if foto is not None and hasattr(foto, "read") else None
    db = SessionLocal()
    try:
        pid, ptxt = service.resolver_actor(db, form.get("reportante"))
        aviso = service.crear_aviso(
            db, equipo_id=int(form.get("equipo_id") or 0), condicion=form.get("condicion") or "",
            origen="web", detectado_at=_parse_dt(form.get("detectado_at")),
            respaldo_entro=form.get("respaldo_entro"), descripcion=form.get("descripcion"),
            reportado_por_id=pid, reportante_texto=ptxt,
            foto=contenido if contenido and len(contenido) <= 3 * 1024 * 1024 else None,
        )
        db.commit()
        return _volver(f"{PREFIX}/tablero",
                       msg=f"Aviso {rules.folio_aviso(aviso.id)} registrado · sugerida {aviso.prioridad_sugerida}.")
    except (ErrorValidacion, ValueError) as e:
        db.rollback()
        destino = f"{PREFIX}/avisos/nuevo" + (f"?equipo_id={form.get('equipo_id')}" if form.get("equipo_id") else "")
        return _volver(destino, err=str(e))
    finally:
        db.close()


@router.post("/avisos/aceptar")
async def avisos_aceptar(request: Request):
    form = await request.form()
    db = SessionLocal()
    try:
        ids = [int(x) for x in form.getlist("aviso_id") if str(x).isdigit()]
        r = service.aceptar_avisos(db, ids, estado_inicial=form.get("estado_inicial") or "pendiente",
                                   cuando=_parse_dt(form.get("cuando")), actor_texto=form.get("actor"))
        db.commit()
        partes = []
        if r["creadas"]:
            partes.append(f"{len(r['creadas'])} OT creada{'s' if len(r['creadas']) != 1 else ''} "
                          f"({', '.join(rules.folio_ot(o.id) for o in r['creadas'])})")
        if r["unidos"]:
            partes.append(f"{len(r['unidos'])} aviso{'s' if len(r['unidos']) != 1 else ''} unido"
                          f"{'s' if len(r['unidos']) != 1 else ''} a su OT abierta")
        return _volver(f"{PREFIX}/tablero", msg="Recibido: " + " · ".join(partes) + ".")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/tablero", err=str(e))
    finally:
        db.close()


@router.post("/avisos/{aviso_id}/unir")
async def aviso_unir(request: Request, aviso_id: int):
    form = await request.form()
    db = SessionLocal()
    try:
        aviso = db.get(MntAviso, aviso_id)
        ot = db.get(MntOt, int(form.get("ot_id") or 0))
        if aviso is None or ot is None:
            return _volver(f"{PREFIX}/tablero", err="Aviso u OT inexistente.")
        service.unir_aviso(db, aviso, ot, actor_texto=form.get("actor"))
        db.commit()
        return _volver(f"{PREFIX}/tablero", msg=f"{rules.folio_aviso(aviso.id)} unido a {rules.folio_ot(ot.id)}.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/tablero", err=str(e))
    finally:
        db.close()


@router.post("/avisos/{aviso_id}/descartar")
async def aviso_descartar(request: Request, aviso_id: int):
    form = await request.form()
    db = SessionLocal()
    try:
        aviso = db.get(MntAviso, aviso_id)
        if aviso is None:
            return _volver(f"{PREFIX}/tablero", err="Ese aviso no existe.")
        service.descartar_aviso(db, aviso, form.get("motivo"))
        db.commit()
        return _volver(f"{PREFIX}/tablero", msg=f"Aviso {rules.folio_aviso(aviso.id)} descartado.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/tablero", err=str(e))
    finally:
        db.close()


@router.get("/avisos/{aviso_id}/foto")
def aviso_foto(aviso_id: int):
    db = SessionLocal()
    try:
        a = db.get(MntAviso, aviso_id)
        if a is None or not a.foto:
            return Response(status_code=404)
        return Response(content=a.foto, media_type="image/jpeg")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# OT: detalle y cambios
# ---------------------------------------------------------------------------
@router.get("/ots/{ot_id}", response_class=HTMLResponse)
def ot_detalle(request: Request, ot_id: int):
    db = SessionLocal()
    try:
        ot = db.get(MntOt, ot_id)
        if ot is None:
            return _volver(f"{PREFIX}/tablero", err="Esa OT no existe.")
        ahora = datetime.now()
        eventos = service.eventos_de(db, ot.id)
        equipo = db.get(MntEquipo, ot.equipo_id)
        return _render(
            "mantenimiento_ot.html", request, active="tablero",
            fila=_fila_ot(ot, equipo, eventos, ahora), ot=ot, equipo=equipo, eventos=eventos,
            avisos=db.query(MntAviso).filter(MntAviso.ot_id == ot.id).order_by(MntAviso.detectado_at).all(),
            personas_map={p.id: p.nombre for p in service.personas(db, solo_activas=False)},
            tecnicos=[p for p in service.personas(db) if p.rol == "tecnico"],
            contratistas=service.contratistas(db),
            contratista=db.get(MntContratista, ot.contratista_id) if ot.contratista_id else None,
            tecnico=db.get(MntPersona, ot.tecnico_id) if ot.tecnico_id else None,
            ot_origen=db.get(MntOt, ot.ot_origen_id) if ot.ot_origen_id else None,
            ot_derivadas=db.query(MntOt).filter(MntOt.ot_origen_id == ot.id).all(),
            ahora=ahora, nombres_personas=[p.nombre for p in service.personas(db)],
            **_ot_ctx(),
        )
    finally:
        db.close()


@router.post("/ots/{ot_id}/estado")
async def ot_estado(request: Request, ot_id: int):
    form = await request.form()
    db = SessionLocal()
    try:
        ot = db.get(MntOt, ot_id)
        if ot is None:
            return _volver(f"{PREFIX}/tablero", err="Esa OT no existe.")
        service.cambiar_estado(db, ot, form.get("estado") or "", cuando=_parse_dt(form.get("cuando")),
                               actor_texto=form.get("actor"), nota=form.get("nota"))
        db.commit()
        return _volver(f"{PREFIX}/ots/{ot_id}", msg=f"{rules.folio_ot(ot.id)}: {rules.ESTADOS_OT[ot.estado]}.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/ots/{ot_id}", err=str(e))
    finally:
        db.close()


@router.post("/ots/{ot_id}/prioridad")
async def ot_prioridad(request: Request, ot_id: int):
    form = await request.form()
    db = SessionLocal()
    try:
        ot = db.get(MntOt, ot_id)
        if ot is None:
            return _volver(f"{PREFIX}/tablero", err="Esa OT no existe.")
        hubo = service.cambiar_prioridad(db, ot, form.get("prioridad") or "", form.get("motivo"), form.get("actor"))
        db.commit()
        return _volver(f"{PREFIX}/ots/{ot_id}", msg="Prioridad cambiada." if hubo else "La prioridad no cambió.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/ots/{ot_id}", err=str(e))
    finally:
        db.close()


@router.post("/ots/{ot_id}/ejecutor")
async def ot_ejecutor(request: Request, ot_id: int):
    form = await request.form()
    db = SessionLocal()
    try:
        ot = db.get(MntOt, ot_id)
        if ot is None:
            return _volver(f"{PREFIX}/tablero", err="Esa OT no existe.")
        service.asignar_ejecutor(db, ot, form.get("ejecutor_tipo") or None, form.get("tecnico_id"),
                                 form.get("contratista_id"), form.get("actor"))
        db.commit()
        return _volver(f"{PREFIX}/ots/{ot_id}", msg="Ejecutor asignado.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/ots/{ot_id}", err=str(e))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Equipos
# ---------------------------------------------------------------------------
@router.get("/equipos", response_class=HTMLResponse)
def equipos_lista(request: Request, sitio: str = "", sistema_id: str = "", baja: str = ""):
    db = SessionLocal()
    try:
        sitio = sitio if sitio in SITIOS else ""
        sis_id = int(sistema_id) if sistema_id.isdigit() else None
        lista = service.equipos(db, sitio or None, sis_id, incluir_baja=bool(baja))
        sistemas = {s.id: s.nombre for s in service.sistemas(db, solo_activos=False)}
        tipos = {t.id: t.nombre for t in service.tipos(db, solo_activos=False)}
        todos = service.equipos(db)
        conteo = {
            "total": len(todos),
            "crianza": sum(1 for e in todos if e.sitio == "crianza"),
            "planta": sum(1 for e in todos if e.sitio == "planta"),
            "A": sum(1 for e in todos if e.criticidad == "A"),
        }
        return _render(
            "mantenimiento_equipos.html", request, active="equipos",
            equipos=lista, sistemas_map=sistemas, tipos_map=tipos,
            sistemas=service.sistemas(db, solo_activos=False), conteo=conteo,
            f_sitio=sitio, f_sistema=sis_id, f_baja=bool(baja),
        )
    finally:
        db.close()


@router.get("/equipos/nuevo", response_class=HTMLResponse)
def equipo_nuevo_form(request: Request, sitio: str = "crianza", desde: Optional[int] = None):
    """Alta de equipo. Con `desde=<id>` es una **redundancia**: el formulario
    llega precargado con la copia del equipo original (spec: equipos gemelos)."""
    db = SessionLocal()
    try:
        extra = {}
        original = db.get(MntEquipo, desde) if desde else None
        if original is not None:
            datos, resp = service.datos_redundancia(db, original)
            sitio = original.sitio
            extra = {"previo": datos, "previo_resp": resp, "destinos_sel": datos["destinos"],
                     "redundancia_de": original,
                     "original_con_respaldo": bool(original.respaldo_equipo_id)}
        return _render(
            "mantenimiento_equipo_nuevo.html", request, active="equipos",
            sitio=sitio if sitio in SITIOS else "crianza",
            siguiente=rules.siguiente_codigo([c for (c,) in db.query(MntEquipo.codigo).all()]),
            **_ficha_ctx(db), **extra,
        )
    finally:
        db.close()


@router.post("/equipos/nuevo")
async def equipo_nuevo(request: Request):
    form = await request.form()
    datos = dict(form)
    datos["destinos"] = form.getlist("destino")
    respuestas = _respuestas(form)
    db = SessionLocal()
    try:
        equipo = service.crear_equipo(db, datos, respuestas, origen="crianza",
                                      autor=datos.get("autor"))
        msg = f"Equipo {equipo.codigo} creado · criticidad {equipo.criticidad}."
        original = db.get(MntEquipo, int(datos["redundancia_de"])) if datos.get("redundancia_de", "").isdigit() else None
        if original is not None and datos.get("respaldo_mutuo"):
            if service.vincular_respaldo_mutuo(db, original, equipo):
                msg += (f" {original.codigo} quedó respaldado por {equipo.codigo}: revisa su criticidad,"
                        f" porque su encuesta puede seguir diciendo «sin respaldo».")
            else:
                msg += f" {original.codigo} ya tenía respaldo y no se cambió."
        db.commit()
        return _volver(f"{PREFIX}/equipos/{equipo.id}", msg=msg)
    except ErrorValidacion as e:
        db.rollback()
        # Se vuelve a mostrar el formulario con lo que ya se había escrito:
        # perder una ficha completa por una respuesta faltante desanima a usarla.
        original = db.get(MntEquipo, int(datos["redundancia_de"])) if datos.get("redundancia_de", "").isdigit() else None
        return _render(
            "mantenimiento_equipo_nuevo.html", request, active="equipos",
            sitio=datos.get("sitio") if datos.get("sitio") in SITIOS else "crianza",
            siguiente=rules.siguiente_codigo([c for (c,) in db.query(MntEquipo.codigo).all()]),
            previo=datos, previo_resp=respuestas, destinos_sel=datos["destinos"], error_form=str(e),
            redundancia_de=original,
            original_con_respaldo=bool(original and original.respaldo_equipo_id),
            **_ficha_ctx(db),
        )
    finally:
        db.close()


@router.get("/equipos/{equipo_id}", response_class=HTMLResponse)
def equipo_ficha(request: Request, equipo_id: int):
    db = SessionLocal()
    try:
        equipo = db.get(MntEquipo, equipo_id)
        if equipo is None:
            return _volver(f"{PREFIX}/equipos", err="Ese equipo no existe.")
        ev = service.ultima_evaluacion(db, equipo.id)
        destinos = service.destinos_de(db, equipo.id)
        return _render(
            "mantenimiento_equipo.html", request, active="equipos",
            equipo=equipo, evaluacion=ev,
            historial=service.historial_criticidad(db, equipo.id),
            destinos_sel=destinos, destinos_labels=service.etiquetas_destinos(db, destinos),
            respaldo_incoherente=service.respaldo_incoherente(equipo, ev),
            ots_equipo=db.query(MntOt).filter(MntOt.equipo_id == equipo.id)
                        .order_by(MntOt.inicio_at.desc()).limit(30).all(),
            **_ot_ctx(),
            respaldo=db.get(MntEquipo, equipo.respaldo_equipo_id) if equipo.respaldo_equipo_id else None,
            respaldado_por_este=[e for e in service.equipos(db) if e.respaldo_equipo_id == equipo.id],
            **_ficha_ctx(db, excluir_id=equipo.id),
        )
    finally:
        db.close()


@router.post("/equipos/{equipo_id}")
async def equipo_editar(request: Request, equipo_id: int):
    form = await request.form()
    db = SessionLocal()
    try:
        equipo = db.get(MntEquipo, equipo_id)
        if equipo is None:
            return _volver(f"{PREFIX}/equipos", err="Ese equipo no existe.")
        datos = dict(form)
        datos["destinos"] = form.getlist("destino")
        service.actualizar_equipo(db, equipo, datos)
        db.commit()
        return _volver(f"{PREFIX}/equipos/{equipo_id}", msg="Ficha guardada.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/equipos/{equipo_id}", err=str(e))
    finally:
        db.close()


@router.post("/equipos/{equipo_id}/criticidad")
async def equipo_criticidad(request: Request, equipo_id: int):
    form = await request.form()
    db = SessionLocal()
    try:
        equipo = db.get(MntEquipo, equipo_id)
        if equipo is None:
            return _volver(f"{PREFIX}/equipos", err="Ese equipo no existe.")
        antes = equipo.criticidad
        hubo = service.reevaluar_criticidad(db, equipo, _respuestas(form), form.get("autor"))
        db.commit()
        if not hubo:
            return _volver(f"{PREFIX}/equipos/{equipo_id}", msg="Las respuestas no cambiaron; no se registró una evaluación nueva.")
        cambio = f"{antes} → {equipo.criticidad}" if antes != equipo.criticidad else f"sigue en {equipo.criticidad}"
        return _volver(f"{PREFIX}/equipos/{equipo_id}", msg=f"Criticidad reevaluada: {cambio}.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/equipos/{equipo_id}", err=str(e))
    finally:
        db.close()


@router.post("/equipos/{equipo_id}/baja")
def equipo_baja(equipo_id: int, accion: str = Form("baja")):
    db = SessionLocal()
    try:
        equipo = db.get(MntEquipo, equipo_id)
        if equipo is None:
            return _volver(f"{PREFIX}/equipos", err="Ese equipo no existe.")
        service.cambiar_baja(db, equipo, accion == "baja")
        db.commit()
        texto = "dado de baja" if accion == "baja" else "reactivado"
        return _volver(f"{PREFIX}/equipos/{equipo_id}", msg=f"Equipo {equipo.codigo} {texto}.")
    finally:
        db.close()


@router.post("/equipos/{equipo_id}/foto")
async def equipo_foto(equipo_id: int, foto: UploadFile = File(...)):
    contenido = await foto.read()
    db = SessionLocal()
    try:
        equipo = db.get(MntEquipo, equipo_id)
        if equipo is None:
            return _volver(f"{PREFIX}/equipos", err="Ese equipo no existe.")
        service.guardar_foto(equipo, contenido, foto.content_type or "")
        db.commit()
        return _volver(f"{PREFIX}/equipos/{equipo_id}", msg="Foto guardada.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/equipos/{equipo_id}", err=str(e))
    finally:
        db.close()


@router.get("/equipos/{equipo_id}/foto")
def equipo_foto_ver(equipo_id: int):
    db = SessionLocal()
    try:
        equipo = db.get(MntEquipo, equipo_id)
        if equipo is None or not equipo.foto:
            return Response(status_code=404)
        return Response(content=equipo.foto, media_type=equipo.foto_mime or "image/jpeg",
                        headers={"Cache-Control": "no-cache"})
    finally:
        db.close()


def _respuestas(form) -> dict:
    return {k: form.get(f"q_{k}") for k in ("impacto", "respaldo", "reposicion", "seguridad_ambiente")}


# ---------------------------------------------------------------------------
# Hoja de QR
# ---------------------------------------------------------------------------
@router.get("/qr", response_class=HTMLResponse)
def qr_hoja(request: Request, sitio: str = "crianza", codigos: str = ""):
    db = SessionLocal()
    try:
        sitio = sitio if sitio in SITIOS else "crianza"
        bot = _bot_username()
        lista = service.equipos(db, sitio)
        if codigos:
            pedidos = {c.strip().upper() for c in codigos.split(",") if c.strip()}
            lista = [e for e in lista if e.codigo in pedidos]
        etiquetas = []
        if bot:
            for e in lista:
                url = f"https://t.me/{bot}?start={e.codigo}"
                etiquetas.append({
                    "codigo": e.codigo, "nombre": e.nombre, "ubicacion": e.ubicacion_texto,
                    # make_qr fuerza QR estándar (como en calidad de agua): la
                    # cámara del teléfono no siempre lee Micro QR.
                    "svg": segno.make_qr(url, error="m").svg_data_uri(scale=4),
                })
        return _render("mantenimiento_qr.html", request, active="qr", sitio=sitio,
                       bot=bot, etiquetas=etiquetas, total=len(lista))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------
@router.get("/personas", response_class=HTMLResponse)
def personas_lista(request: Request, editar: Optional[int] = None, inactivas: str = ""):
    db = SessionLocal()
    try:
        lista = service.personas(db, solo_activas=not inactivas)
        return _render(
            "mantenimiento_personas.html", request, active="personas",
            personas=lista, editando=db.get(MntPersona, editar) if editar else None,
            advertencias=service.advertencias_personas(db),
            dias=list(enumerate(rules.DIAS_LABELS)), fmt_hora=rules.formato_hora,
            f_inactivas=bool(inactivas), sitios=SITIOS,
        )
    finally:
        db.close()


@router.post("/personas")
async def persona_guardar(request: Request):
    form = await request.form()
    datos = dict(form)
    datos["alarmas_sitios"] = form.getlist("alarmas_sitios")
    datos["horario_dias"] = form.getlist("horario_dias")
    persona_id = datos.get("persona_id")
    db = SessionLocal()
    try:
        persona = db.get(MntPersona, int(persona_id)) if persona_id else None
        persona = service.guardar_persona(db, datos, persona)
        db.commit()
        return _volver(f"{PREFIX}/personas", msg=f"{persona.nombre} guardado.")
    except ErrorValidacion as e:
        db.rollback()
        destino = f"{PREFIX}/personas" + (f"?editar={persona_id}" if persona_id else "")
        return _volver(destino, err=str(e))
    finally:
        db.close()


@router.post("/personas/{persona_id}/activo")
def persona_activo(persona_id: int, activo: str = Form("0")):
    db = SessionLocal()
    try:
        p = db.get(MntPersona, persona_id)
        if p is not None:
            p.activo = activo == "1"
            db.commit()
        return _volver(f"{PREFIX}/personas", msg="Persona actualizada.")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Contratistas
# ---------------------------------------------------------------------------
@router.get("/contratistas", response_class=HTMLResponse)
def contratistas_lista(request: Request, editar: Optional[int] = None):
    db = SessionLocal()
    try:
        lista = service.contratistas(db, solo_activos=False)
        uso = {}
        for e in service.equipos(db):
            if e.contratista_habitual_id:
                uso[e.contratista_habitual_id] = uso.get(e.contratista_habitual_id, 0) + 1
        return _render(
            "mantenimiento_contratistas.html", request, active="contratistas",
            contratistas=lista, uso=uso,
            editando=db.get(MntContratista, editar) if editar else None,
        )
    finally:
        db.close()


@router.post("/contratistas")
async def contratista_guardar(request: Request):
    form = await request.form()
    cid = form.get("contratista_id")
    db = SessionLocal()
    try:
        c = db.get(MntContratista, int(cid)) if cid else None
        c = service.guardar_contratista(db, dict(form), c)
        db.commit()
        return _volver(f"{PREFIX}/contratistas", msg=f"{c.empresa} guardado.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/contratistas", err=str(e))
    finally:
        db.close()


@router.post("/contratistas/{cid}/activo")
def contratista_activo(cid: int, activo: str = Form("0")):
    db = SessionLocal()
    try:
        c = db.get(MntContratista, cid)
        if c is not None:
            c.activo = activo == "1"
            db.commit()
        return _volver(f"{PREFIX}/contratistas", msg="Contratista actualizado.")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Catálogos (sistemas y tipos) y parámetros
# ---------------------------------------------------------------------------
@router.get("/catalogos", response_class=HTMLResponse)
def catalogos(request: Request):
    db = SessionLocal()
    try:
        todos = service.equipos(db, incluir_baja=True)
        uso_sis, uso_tipo = {}, {}
        for e in todos:
            uso_sis[e.sistema_id] = uso_sis.get(e.sistema_id, 0) + 1
            uso_tipo[e.tipo_id] = uso_tipo.get(e.tipo_id, 0) + 1
        params = db.query(MntParametro).order_by(MntParametro.id).all()
        return _render(
            "mantenimiento_catalogos.html", request, active="catalogos",
            sistemas=service.sistemas(db, solo_activos=False),
            tipos=service.tipos(db, solo_activos=False),
            uso_sis=uso_sis, uso_tipo=uso_tipo, parametros=params, sitios=SITIOS,
        )
    finally:
        db.close()


@router.post("/catalogos/sistemas")
def sistema_nuevo(nombre: str = Form(""), sitio: str = Form(""), orden: str = Form("")):
    db = SessionLocal()
    try:
        service.crear_sistema(db, nombre, sitio, int(orden) if orden.strip().isdigit() else None)
        db.commit()
        return _volver(f"{PREFIX}/catalogos", msg=f"Sistema «{nombre.strip()}» agregado.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/catalogos", err=str(e))
    finally:
        db.close()


@router.post("/catalogos/tipos")
def tipo_nuevo(nombre: str = Form("")):
    db = SessionLocal()
    try:
        service.crear_tipo(db, nombre)
        db.commit()
        return _volver(f"{PREFIX}/catalogos", msg=f"Tipo «{nombre.strip()}» agregado.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/catalogos", err=str(e))
    finally:
        db.close()


@router.post("/catalogos/{que}/rapido")
def catalogo_rapido(que: str, nombre: str = Form(""), sitio: str = Form("")):
    """Crear un sistema o tipo desde la misma ficha del equipo, sin ir a
    Catálogos: así el catálogo crece cuando hace falta. Responde JSON para que
    la ficha agregue la opción sin recargar (y sin perder lo ya escrito)."""
    db = SessionLocal()
    try:
        if que == "sistemas":
            item = service.crear_sistema(db, nombre, sitio, None)
        elif que == "tipos":
            item = service.crear_tipo(db, nombre)
        else:
            return JSONResponse({"error": "Catálogo desconocido."}, status_code=400)
        db.commit()
        return JSONResponse({"id": item.id, "nombre": item.nombre,
                             "sitio": getattr(item, "sitio", None)})
    except ErrorValidacion as e:
        db.rollback()
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        db.close()


@router.post("/catalogos/sistemas/{sistema_id}/editar")
def sistema_editar(sistema_id: int, nombre: str = Form(""), sitio: str = Form(""), orden: str = Form("")):
    db = SessionLocal()
    try:
        s = db.get(MntSistema, sistema_id)
        if s is None:
            return _volver(f"{PREFIX}/catalogos", err="Ese sistema no existe.")
        service.actualizar_sistema(db, s, nombre, sitio, int(orden) if orden.strip().lstrip("-").isdigit() else None)
        db.commit()
        return _volver(f"{PREFIX}/catalogos", msg=f"Sistema «{s.nombre}» guardado.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/catalogos", err=str(e))
    finally:
        db.close()


@router.post("/catalogos/{que}/{item_id}/fusionar")
def catalogo_fusionar(que: str, item_id: int, destino_id: str = Form("")):
    if que not in ("sistemas", "tipos"):
        return _volver(f"{PREFIX}/catalogos", err="Catálogo desconocido.")
    if not destino_id.isdigit():
        return _volver(f"{PREFIX}/catalogos", err="Elige en cuál fusionar.")
    db = SessionLocal()
    try:
        origen, destino, n = service.fusionar(db, que, item_id, int(destino_id))
        db.commit()
        return _volver(f"{PREFIX}/catalogos",
                       msg=f"«{origen}» fusionado en «{destino}»: {n} equipo{'s' if n != 1 else ''} movido{'s' if n != 1 else ''}.")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/catalogos", err=str(e))
    finally:
        db.close()


@router.post("/catalogos/{que}/{item_id}/activo")
def catalogo_activo(que: str, item_id: int, activo: str = Form("0")):
    modelo = {"sistemas": MntSistema, "tipos": MntTipoEquipo}.get(que)
    if modelo is None:
        return _volver(f"{PREFIX}/catalogos", err="Catálogo desconocido.")
    db = SessionLocal()
    try:
        item = db.get(modelo, item_id)
        if item is not None:
            item.activo = activo == "1"
            db.commit()
        return _volver(f"{PREFIX}/catalogos", msg="Catálogo actualizado.")
    finally:
        db.close()


@router.post("/parametros")
async def parametros_guardar(request: Request):
    form = await request.form()
    db = SessionLocal()
    try:
        n = service.actualizar_parametros(db, dict(form))
        db.commit()
        return _volver(f"{PREFIX}/catalogos", msg=f"Parámetros guardados ({n} cambio{'s' if n != 1 else ''}).")
    except ErrorValidacion as e:
        db.rollback()
        return _volver(f"{PREFIX}/catalogos", err=str(e))
    finally:
        db.close()
