"""Mensajes salientes del módulo de mantenimiento (spec §6.4).

En PR3:
- **Alarma P1** inmediata, a cualquier hora, a quien corresponda según el sitio:
  las personas con ese sitio en `alarmas_sitios` (el encargado, el supervisor
  de Planta) y, en Crianza, además el **semanero de turno** de la rotación de
  calidad de agua.
- **Confirmación al que avisó**: cuando su aviso se acepta y cuando la OT se
  cierra. Es barata y cambia la cultura: quien avisa ve que sirvió.

Las fallas menores (P2/P3) no generan mensajes sueltos: van al resumen (PR4).

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
