"""Configuración → Personas y notificaciones.

El directorio único de personas (identidad) y la navegación hacia las
preferencias de cada módulo:

    Directorio        /views/ui/config/personas                     (aquí)
    Calidad de agua   /views/ui/calidad-agua/alertas/destinatarios  (wq_alerts_views)
    Semaneros         /views/ui/calidad-agua/alertas/semaneros      (wq_alerts_views)
    Mantenimiento     /views/ui/mantenimiento/personas              (maintenance.router)

Las pestañas de módulo conservan su URL (y su lógica) pero se muestran dentro
de Configuración: quién recibe qué es configuración, no trabajo diario.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import func

from app.db.session import SessionLocal
from app.maintenance import service as mnt
from app.maintenance.models import MntAviso, MntTelegramContacto
from app.models.personas import Persona
from app.services import personas as directorio

PREFIX = "/views/ui/config/personas"
router = APIRouter(prefix=PREFIX, tags=["personas"])
jinja_env = Environment(loader=FileSystemLoader(str(Path(__file__).parent.parent / "templates")))

_TABS = [
    ("directorio", "Directorio", PREFIX),
    ("calidad", "Calidad de agua", "/views/ui/calidad-agua/alertas/destinatarios"),
    ("semaneros", "Semaneros", "/views/ui/calidad-agua/alertas/semaneros"),
    ("mantenimiento", "Mantenimiento", "/views/ui/mantenimiento/personas"),
]


def tabs_personas(activa: str) -> list[dict]:
    """Pestañas de «Personas y notificaciones», en el formato de `_alertas_tabs.html`
    (lista de dicts) para que las usen también las páginas de calidad de agua."""
    return [{"href": h, "label": l, "activa": k == activa} for k, l, h in _TABS]


def _volver(url: str, msg: Optional[str] = None, err: Optional[str] = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote_plus(msg)}"
    elif err:
        url += f"{sep}err={quote_plus(err)}"
    return RedirectResponse(url=url, status_code=303)


@router.get("", response_class=HTMLResponse)
def directorio_page(request: Request, editar: Optional[int] = None, inactivas: str = ""):
    db = SessionLocal()
    try:
        lista = directorio.listar(db, solo_activas=not inactivas)
        iniciados = {u for (u,) in db.query(MntTelegramContacto.telegram_user_id).all()}
        sin_vincular = mnt.contactos_sin_vincular(db)
        n_avisos = dict(db.query(MntAviso.telegram_chat_id, func.count(MntAviso.id))
                        .filter(MntAviso.telegram_chat_id.in_([c.telegram_user_id for c in sin_vincular] or [""]))
                        .group_by(MntAviso.telegram_chat_id).all())
        html = jinja_env.get_template("config_personas.html").render(
            request=request, msg=request.query_params.get("msg"), err=request.query_params.get("err"),
            tabs=tabs_personas("directorio"), prefix=PREFIX,
            personas=lista, participa=directorio.participacion(db), iniciados=iniciados,
            editando=db.get(Persona, editar) if editar else None,
            sin_vincular=sin_vincular, n_avisos=n_avisos,
            sin_telegram=[p for p in directorio.listar(db) if not p.telegram_id],
            roles=mnt.ROLES_PERSONA, f_inactivas=bool(inactivas))
        return HTMLResponse(html)
    finally:
        db.close()


@router.post("")
async def persona_guardar(request: Request):
    form = await request.form()
    db = SessionLocal()
    pid = form.get("persona_id")
    try:
        p = db.get(Persona, int(pid)) if pid and pid.isdigit() else None
        datos = dict(form)
        datos["activo"] = True if p is None else p.activo
        p = directorio.guardar(db, datos, p)
        db.commit()
        return _volver(PREFIX, msg=f"{p.nombre} guardado.")
    except directorio.ErrorPersona as e:
        db.rollback()
        return _volver(PREFIX + (f"?editar={pid}" if pid else ""), err=str(e))
    finally:
        db.close()


@router.post("/{persona_id}/activo")
def persona_activo(persona_id: int, activo: str = Form("0")):
    db = SessionLocal()
    try:
        p = db.get(Persona, persona_id)
        if p is not None:
            p.activo = activo == "1"
            p.updated_at = datetime.now()
            if not p.activo:
                # En cascada: calidad de agua filtra por su propio `active` en
                # varias consultas (semaneros, despacho), así que se apaga ahí
                # también. Reactivar la persona NO reactiva los módulos: cada
                # uno decide de nuevo en su pestaña.
                part = directorio.participacion(db).get(p.id, {})
                for pref in (part.get("wq"), part.get("mnt")):
                    if pref is not None:
                        setattr(pref, "active" if hasattr(pref, "in_rotation") else "activo", False)
            db.commit()
        return _volver(PREFIX, msg=("Persona activada. Revisa en cada módulo si debe volver a recibir avisos."
                                    if activo == "1" else
                                    "Persona desactivada: dejó de recibir avisos de todos los módulos."))
    finally:
        db.close()


@router.post("/telegram/{contacto_id}/vincular")
async def contacto_vincular(request: Request, contacto_id: int):
    form = await request.form()
    db = SessionLocal()
    try:
        pid = form.get("persona_id")
        p = mnt.vincular_contacto(db, contacto_id,
                                  persona_id=int(pid) if pid and pid.isdigit() else None,
                                  nombre_nuevo=form.get("nombre_nuevo"), rol=form.get("rol") or "reportante")
        db.commit()
        return _volver(PREFIX, msg=f"Telegram vinculado a {p.nombre}.")
    except mnt.ErrorValidacion as e:
        db.rollback()
        return _volver(PREFIX, err=str(e))
    finally:
        db.close()


@router.post("/telegram/{contacto_id}/bloquear")
def contacto_bloquear(contacto_id: int, bloquear: str = Form("1")):
    db = SessionLocal()
    try:
        c = db.get(MntTelegramContacto, contacto_id)
        if c is not None:
            c.bloqueado = bloquear == "1"
            db.commit()
        return _volver(PREFIX, msg="Contacto bloqueado: el bot lo ignorará." if bloquear == "1"
                       else "Contacto desbloqueado.")
    finally:
        db.close()
