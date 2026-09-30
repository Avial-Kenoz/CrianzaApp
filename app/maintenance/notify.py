"""Mensajes salientes del módulo de mantenimiento (spec §6.4).

En PR3:
- **Alarma P1** inmediata, a cualquier hora, a quien corresponda según el sitio:
  las personas con ese sitio en `alarmas_sitios` (el encargado, el supervisor
  de Planta) y, en Crianza, además el **semanero de turno** de la rotación de
  calidad de agua.
- **Confirmación al que avisó**: cuando su aviso se acepta y cuando la OT se
  cierra. Es barata y cambia la cultura: quien avisa ve que sirvió.

En PR4:
- **Resumen** de fallas menores (P2/P3), OT abiertas y fuera de plazo, y
  servicios sin respaldo, a quien tiene `recibe_resumen`, en las horas de
  `resumen_horas` y solo dentro de su horario. Uno por corte y por persona.
- **Repetición de P1 sin movimiento**: si una OT P1 (o un aviso P1 sin
  atender) pasa `p1_repetir_horas` sin cambios, se avisa una vez más.

Las fallas menores no generan mensajes sueltos: van al resumen.

Cada intento queda en `mnt_notificaciones` con su resultado. Los envíos corren
en un hilo aparte con su propia sesión: una red lenta no puede dejar colgada
la página que registró el aviso, y un error de Telegram nunca deshace lo que
ya se guardó.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.maintenance import rules
from app.maintenance.models import (
    SITIO_LABELS, MntAviso, MntEquipo, MntNotificacion, MntOt,
)
from app.maintenance.telegram_api import Api, TelegramError

logger = logging.getLogger("mnt_notify")

BASE_URL = (os.getenv("CRIANZA_BASE_URL") or "http://192.168.1.201:8002").rstrip("/")
URL_TABLERO = BASE_URL + "/views/ui/mantenimiento/tablero"
URL_PARTE = BASE_URL + "/views/ui/mantenimiento/parte"
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def _registrar(db: Session, *, motivo: str, ok: bool, error: Optional[str] = None,
               chat_id: Optional[str] = None, destino: Optional[str] = None,
               persona_id: Optional[int] = None, aviso_id: Optional[int] = None,
               ot_id: Optional[int] = None) -> None:
    db.add(MntNotificacion(motivo=motivo, ok=ok, error=(error or "")[:300] or None,
                           chat_id=chat_id, destino=destino, persona_id=persona_id,
                           aviso_id=aviso_id, ot_id=ot_id, enviado_at=datetime.now()))


def _enviar(db: Session, api: Api, *, chat_id: str, texto: str, motivo: str, destino: str,
            persona_id=None, aviso_id=None, ot_id=None) -> bool:
    try:
        api.enviar(chat_id, texto)
        _registrar(db, motivo=motivo, ok=True, chat_id=chat_id, destino=destino,
                   persona_id=persona_id, aviso_id=aviso_id, ot_id=ot_id)
        return True
    except TelegramError as e:
        logger.warning("mnt: no se pudo avisar a %s (%s): %s", destino, motivo, e)
        _registrar(db, motivo=motivo, ok=False, error=str(e), chat_id=chat_id, destino=destino,
                   persona_id=persona_id, aviso_id=aviso_id, ot_id=ot_id)
        return False


# ---------------------------------------------------------------------------
# A quién le toca
# ---------------------------------------------------------------------------
def destinatarios_alarma(db: Session, sitio: str, ahora: Optional[datetime] = None) -> list[dict]:
    """[{persona_id, nombre, chat_id}] sin repetir chat.

    El semanero sale de la rotación de calidad de agua en el momento de la
    alarma. Su chat es el mismo número que su usuario de Telegram, pero el bot
    de mantenimiento solo puede escribirle si le hizo /start (spec §6.4): si no,
    el envío falla con 403 y queda en la bitácora.
    """
    from app.maintenance.service import personas as roles_activos

    ahora = ahora or datetime.now()
    out, vistos = [], set()
    for p in roles_activos(db):                  # activos en el módulo y en el directorio
        if sitio in (p.alarmas_sitios or "").split(",") and p.telegram_user_id:
            if p.telegram_user_id not in vistos:
                vistos.add(p.telegram_user_id)
                out.append({"persona_id": p.id, "nombre": p.nombre, "chat_id": p.telegram_user_id})
    if sitio == "crianza":
        sem = semanero_actual(db, ahora)
        if sem and sem["chat_id"] not in vistos:
            out.append(sem)
    return out


def semanero_actual(db: Session, ahora: datetime) -> Optional[dict]:
    """Semanero de turno de calidad de agua como destinatario de mantenimiento."""
    try:
        from app.services.wq_notify import semanero_de
        from app.models.water_quality_alerts import WaterQualityAlertRecipient
    except Exception:                                   # módulo ausente (p. ej. otra app)
        return None
    rid = semanero_de(db, ahora)
    if rid is None:
        return None
    r = db.get(WaterQualityAlertRecipient, rid)
    if r is None or not r.telegram_chat_id:
        return None
    # Si el semanero también tiene rol en mantenimiento, se atribuye a él (con
    # el directorio único es la misma persona: se reconoce por su Telegram).
    from app.maintenance.service import rol_de_telegram
    p = rol_de_telegram(db, str(r.telegram_chat_id))
    return {"persona_id": p.id if p else None, "nombre": f"{r.name} (semanero)",
            "chat_id": str(r.telegram_chat_id)}


# ---------------------------------------------------------------------------
# Mensajes
# ---------------------------------------------------------------------------
def texto_alarma(aviso: MntAviso, equipo: MntEquipo) -> str:
    cond = rules.CONDICIONES.get(aviso.condicion, aviso.condicion)
    if aviso.respaldo_entro == "no":
        cond += " · el respaldo NO entró"
    elif aviso.respaldo_entro == "no_se":
        cond += " · no se sabe si entró el respaldo"
    lineas = [
        f"🚨 ALARMA P1 · {SITIO_LABELS.get(equipo.sitio, equipo.sitio)}",
        f"{equipo.codigo} {equipo.nombre}",
        f"🛑 {cond}" if aviso.condicion == "detenido" else f"⚠️ {cond}",
    ]
    if aviso.descripcion:
        lineas.append(f"«{aviso.descripcion[:300]}»")
    quien = f" · avisó {aviso.reportante_texto}" if aviso.reportante_texto else ""
    lineas.append(f"Aviso {rules.folio_aviso(aviso.id)} · detectado {aviso.detectado_at:%H:%M}{quien}")
    lineas.append(f"Tablero: {URL_TABLERO}")
    return "\n".join(lineas)


def alarma_aviso(db: Session, aviso_id: int, api: Optional[Api] = None) -> int:
    """Manda la alarma de un aviso si es P1. Devuelve cuántos la recibieron."""
    aviso = db.get(MntAviso, aviso_id)
    if aviso is None or aviso.prioridad_sugerida != "P1":
        return 0
    equipo = db.get(MntEquipo, aviso.equipo_id)
    api = api or Api()
    dest = destinatarios_alarma(db, equipo.sitio)
    if not dest:
        # Nadie configurado: queda registrado para que se vea en la pantalla de
        # personas, que es donde se arregla.
        _registrar(db, motivo="alarma_p1", ok=False, aviso_id=aviso.id, destino="(nadie)",
                   error=f"Nadie recibe las alarmas P1 de {SITIO_LABELS.get(equipo.sitio)}")
        db.commit()
        return 0
    texto = texto_alarma(aviso, equipo)
    n = sum(_enviar(db, api, chat_id=d["chat_id"], texto=texto, motivo="alarma_p1",
                    destino=d["nombre"], persona_id=d["persona_id"], aviso_id=aviso.id)
            for d in dest)
    db.commit()
    return n


def confirmar_acuse(db: Session, aviso_ids: list[int], api: Optional[Api] = None) -> None:
    """Al que avisó por Telegram: su aviso fue recibido y está en una OT."""
    api = api or Api()
    for a in db.query(MntAviso).filter(MntAviso.id.in_(aviso_ids or []), MntAviso.telegram_chat_id.isnot(None)).all():
        if not a.ot_id:
            continue
        eq = db.get(MntEquipo, a.equipo_id)
        _enviar(db, api, chat_id=a.telegram_chat_id, motivo="confirmacion_acuse",
                destino=a.reportante_texto or "reportante", persona_id=a.reportado_por_id,
                aviso_id=a.id, ot_id=a.ot_id,
                texto=f"👍 Tu aviso {rules.folio_aviso(a.id)} de {eq.codigo} {eq.nombre} fue recibido: "
                      f"se está atendiendo en la {rules.folio_ot(a.ot_id)}.")
    db.commit()


def confirmar_cierre(db: Session, ot_id: int, api: Optional[Api] = None) -> None:
    """Al que avisó por Telegram: el equipo quedó reparado."""
    ot = db.get(MntOt, ot_id)
    if ot is None or ot.estado != "cerrada":
        return
    api = api or Api()
    eq = db.get(MntEquipo, ot.equipo_id)
    hora = (ot.equipo_en_servicio_at or ot.cierre_at)
    chats = {}
    for a in db.query(MntAviso).filter(MntAviso.ot_id == ot.id, MntAviso.telegram_chat_id.isnot(None)).all():
        chats.setdefault(a.telegram_chat_id, a)          # un mensaje por persona
    for chat, a in chats.items():
        _enviar(db, api, chat_id=chat, motivo="confirmacion_cierre",
                destino=a.reportante_texto or "reportante", persona_id=a.reportado_por_id,
                aviso_id=a.id, ot_id=ot.id,
                texto=f"✅ {eq.codigo} {eq.nombre} quedó en servicio"
                      f"{f' a las {hora:%H:%M}' if hora else ''} ({rules.folio_ot(ot.id)}). ¡Gracias por avisar!")
    db.commit()


# ---------------------------------------------------------------------------
# Resumen de fallas menores (PR4)
# ---------------------------------------------------------------------------
def texto_resumen(db: Session, ahora: Optional[datetime] = None, *, vacio_ok: bool = False) -> Optional[str]:
    """El resumen para el encargado. None si no hay nada pendiente (un «todo en
    orden» diario es ruido), salvo `vacio_ok` (envío manual: confirma que llega)."""
    from app.maintenance import service

    ahora = ahora or datetime.now()
    equipos = {e.id: e for e in service.equipos(db, incluir_baja=True)}
    avisos = service.avisos_nuevos(db)
    abiertas = service.ots(db)
    ev = service.eventos_por_ot(db, [o.id for o in abiertas])
    vencidas = [o for o in abiertas if service.ot_fuera_de_plazo(o, ahora)]
    riesgo = service.servicios_en_riesgo(db, ahora)

    def eq(i):
        e = equipos.get(i)
        return f"{e.codigo} {e.nombre}" if e else "?"

    lineas = [f"📋 Resumen de mantenimiento · {DIAS[ahora.weekday()]} {ahora:%d-%m %H:%M}"]
    if avisos:
        lineas.append(f"\nAvisos sin atender: {len(avisos)}")
        for a in avisos[:10]:
            lineas.append(f" • {rules.folio_aviso(a.id)} {eq(a.equipo_id)} · {a.prioridad_sugerida} · "
                          f"hace {rules.formato_duracion((ahora - a.detectado_at).total_seconds() / 3600)}")
        if len(avisos) > 10:
            lineas.append(f"   …y {len(avisos) - 10} más")
    if abiertas:
        por = {p: sum(1 for o in abiertas if o.prioridad == p) for p in ("P1", "P2", "P3")}
        lineas.append(f"\nOT abiertas: {len(abiertas)} (P1 {por['P1']} · P2 {por['P2']} · P3 {por['P3']})")
    if vencidas:
        lineas.append(f"Fuera de plazo: {len(vencidas)}")
        for o in vencidas[:10]:
            ult = ev[o.id][-1].ocurrido_at if ev.get(o.id) else o.inicio_at
            lineas.append(f" • {rules.folio_ot(o.id)} {eq(o.equipo_id)} · {rules.ESTADOS_OT[o.estado]} · "
                          f"sin movimiento hace {rules.formato_duracion((ahora - ult).total_seconds() / 3600)}")
    if riesgo:
        lineas.append("\nServicios sin respaldo:")
        for r in riesgo:
            det = ", ".join(e.codigo for e in r["detenidos"]) or "—"
            lineas.append(f" • {r['grupo'].nombre} ({'sin respaldo' if r['margen'] == 0 else 'faltan equipos'}; detenidos: {det})")
    if len(lineas) == 1:
        if not vacio_ok:
            return None
        lineas.append("\nSin pendientes ✅")
    lineas.append(f"\nParte diario: {URL_PARTE}")
    return "\n".join(lineas)


def destinatarios_resumen(db: Session) -> list:
    """Roles activos con `recibe_resumen` y Telegram."""
    from app.maintenance.service import personas as roles_activos
    return [p for p in roles_activos(db) if p.recibe_resumen and p.telegram_user_id]


def enviar_resumenes(db: Session, api: Optional[Api] = None, ahora: Optional[datetime] = None,
                     *, forzar: bool = False, _solo_roles: Optional[set] = None) -> int:
    """Manda el resumen a quien corresponda. Devuelve cuántos se enviaron.

    Automático (`forzar=False`): solo si hay un corte vigente de
    `resumen_horas`, la persona está dentro de su horario y no se le mandó ya
    en este corte. Manual (`forzar=True`, botón «Enviar resumen ahora»): a
    todos los que reciben el resumen, a cualquier hora, aunque no haya
    pendientes.
    """
    from app.maintenance import service

    ahora = ahora or datetime.now()
    hora = ahora.hour + ahora.minute / 60.0
    if not forzar:
        corte = rules.corte_vigente(hora, rules.parse_cortes(service.parametros(db).get("resumen_horas")))
        if corte is None:
            return 0
        desde_corte = ahora.replace(hour=int(corte), minute=int(round((corte % 1) * 60)), second=0, microsecond=0)
    dest = destinatarios_resumen(db)
    if _solo_roles is not None:          # solo pruebas: no registrar envíos a personas reales
        dest = [p for p in dest if p.id in _solo_roles]
    if not forzar:
        dest = [p for p in dest if rules.en_horario(ahora.weekday(), hora, p.horario_dias, p.horario_desde, p.horario_hasta)
                and not db.query(MntNotificacion.id).filter(
                    MntNotificacion.motivo == "resumen", MntNotificacion.chat_id == p.telegram_user_id,
                    MntNotificacion.ok.is_(True), MntNotificacion.enviado_at >= desde_corte).first()]
    if not dest:
        return 0
    texto = texto_resumen(db, ahora, vacio_ok=forzar)
    if texto is None:
        return 0
    api = api or Api()
    n = sum(_enviar(db, api, chat_id=p.telegram_user_id, texto=texto, motivo="resumen",
                    destino=p.nombre, persona_id=p.id) for p in dest)
    db.commit()
    return n


# ---------------------------------------------------------------------------
# Repetición de P1 sin movimiento (PR4)
# ---------------------------------------------------------------------------
def repetir_p1(db: Session, api: Optional[Api] = None, ahora: Optional[datetime] = None,
               *, _solo_equipos: Optional[set] = None) -> int:
    """Una alarma P1 no puede quedar en silencio: si una OT P1 abierta (o un
    aviso P1 sin atender) pasa `p1_repetir_horas` sin cambios, se avisa UNA vez
    más a los mismos destinatarios de la alarma. Devuelve cuántos mensajes salieron."""
    from app.maintenance import service

    ahora = ahora or datetime.now()
    try:
        umbral = float(str(service.parametros(db).get("p1_repetir_horas") or "2").replace(",", "."))
    except ValueError:
        umbral = 2.0
    ya = lambda **k: db.query(MntNotificacion.id).filter(  # noqa: E731
        MntNotificacion.motivo == "repeticion_p1", *[getattr(MntNotificacion, c) == v for c, v in k.items()]).first()

    # `_solo_equipos` es solo para pruebas: si una prueba marcara como repetida
    # una alarma P1 real, producción ya no la volvería a avisar.
    fuera = (lambda eid: _solo_equipos is not None and eid not in _solo_equipos)  # noqa: E731
    pendientes = []   # (texto, sitio, aviso_id, ot_id)
    for o in service.ots(db):
        if o.prioridad != "P1" or fuera(o.equipo_id) or ya(ot_id=o.id):
            continue
        ult = service._ultima_hora(db, o)
        horas = (ahora - ult).total_seconds() / 3600
        if horas >= umbral:
            e = db.get(MntEquipo, o.equipo_id)
            pendientes.append((f"🔁 P1 sin movimiento hace {rules.formato_duracion(horas)} · {SITIO_LABELS[e.sitio]}\n"
                               f"{rules.folio_ot(o.id)} · {e.codigo} {e.nombre}\n"
                               f"Estado: {rules.ESTADOS_OT[o.estado]}\nTablero: {URL_TABLERO}", e.sitio, None, o.id))
    for a in service.avisos_nuevos(db):
        if a.prioridad_sugerida != "P1" or fuera(a.equipo_id) or ya(aviso_id=a.id):
            continue
        horas = (ahora - a.created_at).total_seconds() / 3600
        if horas >= umbral:
            e = db.get(MntEquipo, a.equipo_id)
            pendientes.append((f"🔁 P1 sin atender hace {rules.formato_duracion(horas)} · {SITIO_LABELS[e.sitio]}\n"
                               f"{rules.folio_aviso(a.id)} · {e.codigo} {e.nombre} · nadie ha hecho el acuse\n"
                               f"Tablero: {URL_TABLERO}", e.sitio, a.id, None))
    if not pendientes:
        return 0
    api = api or Api()
    n = 0
    for texto, sitio, aviso_id, ot_id in pendientes:
        dest = destinatarios_alarma(db, sitio, ahora)
        if not dest:
            _registrar(db, motivo="repeticion_p1", ok=False, aviso_id=aviso_id, ot_id=ot_id, destino="(nadie)",
                       error=f"Nadie recibe las alarmas P1 de {SITIO_LABELS.get(sitio)}")
        for d in dest:
            n += _enviar(db, api, chat_id=d["chat_id"], texto=texto, motivo="repeticion_p1",
                         destino=d["nombre"], persona_id=d["persona_id"], aviso_id=aviso_id, ot_id=ot_id)
    db.commit()
    return n


# ---------------------------------------------------------------------------
# En segundo plano
# ---------------------------------------------------------------------------
def envios_habilitados() -> bool:
    """Los mensajes a personas reales salen SOLO donde el bot está habilitado
    (`MNT_BOT_ENABLED=1`, producción). Pasó el 30-09: pruebas en un servidor de
    desarrollo, contra la base compartida, generaron alarmas P1 dirigidas al
    semanero real; no salieron solo porque ese servidor no tenía el token."""
    return (os.getenv("MNT_BOT_ENABLED") or "").strip() == "1"


def en_segundo_plano(fn, *args) -> None:
    """Corre `fn(db, *args)` en un hilo con su propia sesión. Nunca levanta.
    Fuera de producción no hace nada (ver `envios_habilitados`); las pruebas
    llaman a las funciones directo, con una API falsa."""
    if not envios_habilitados():
        logger.info("mnt: envío omitido (%s): MNT_BOT_ENABLED no es 1", getattr(fn, "__name__", fn))
        return

    def _run():
        db = SessionLocal()
        try:
            fn(db, *args)
        except Exception:
            db.rollback()
            logger.exception("mnt: falló el envío en segundo plano (%s)", getattr(fn, "__name__", fn))
        finally:
            db.close()
    threading.Thread(target=_run, daemon=True, name="mnt-notify").start()


def ultimos_errores(db: Session, limite: int = 5) -> list:
    return (db.query(MntNotificacion).filter(MntNotificacion.ok.is_(False))
            .order_by(MntNotificacion.enviado_at.desc()).limit(limite).all())
