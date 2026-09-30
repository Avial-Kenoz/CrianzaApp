"""Directorio de personas: casos de uso compartidos por todos los módulos.

La identidad (nombre, Telegram, teléfono) se edita solo aquí. Calidad de agua y
mantenimiento guardan sus preferencias apuntando a una `Persona`
(ver app/models/personas.py).
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.personas import Persona


class ErrorPersona(ValueError):
    """Dato inválido; el mensaje se muestra tal cual."""


def clave_nombre(nombre: Optional[str]) -> str:
    """«Andrés Vial» y «andres vial» son el mismo nombre para buscar duplicados."""
    s = unicodedata.normalize("NFKD", (nombre or "").strip().lower())
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def listar(db: Session, solo_activas: bool = True) -> list:
    q = db.query(Persona)
    if solo_activas:
        q = q.filter(Persona.activo.is_(True))
    return q.order_by(Persona.nombre).all()


def por_telegram(db: Session, telegram_id: Optional[str]) -> Optional[Persona]:
    if not telegram_id:
        return None
    return db.query(Persona).filter(Persona.telegram_id == str(telegram_id)).first()


def por_nombre(db: Session, nombre: str) -> Optional[Persona]:
    # Se compara en Python y no con LIKE: un prefiltro SQL fallaría con «Ñuñez»
    # vs «Nunez». Son decenas de personas; no pesa.
    clave = clave_nombre(nombre)
    if not clave:
        return None
    return next((p for p in db.query(Persona).all() if clave_nombre(p.nombre) == clave), None)


def guardar(db: Session, datos: dict, persona: Optional[Persona] = None) -> Persona:
    nombre = " ".join((datos.get("nombre") or "").split())
    if not nombre:
        raise ErrorPersona("La persona necesita un nombre.")
    telegram = (datos.get("telegram_id") or "").strip() or None
    if telegram and not telegram.lstrip("-").isdigit():
        raise ErrorPersona("El ID de Telegram es un número (el que muestra el bot o la bandeja).")
    otra = por_telegram(db, telegram)
    if otra and (persona is None or otra.id != persona.id):
        raise ErrorPersona(f"Ese Telegram ya es de {otra.nombre}.")
    tocayo = por_nombre(db, nombre)
    if persona is None and tocayo and not datos.get("confirmar_tocayo"):
        # Probablemente es la misma persona escrita distinto: mejor avisar que
        # crear un segundo registro que después hay que unir a mano.
        raise ErrorPersona(f"Ya existe «{tocayo.nombre}». Si es otra persona con el mismo nombre, "
                           f"agrégale un segundo apellido o una inicial.")
    now = datetime.now()
    if persona is None:
        persona = Persona(created_at=now, activo=True)
        db.add(persona)
    persona.nombre = nombre[:120]
    persona.telegram_id = telegram
    persona.telefono = (datos.get("telefono") or "").strip()[:40] or None
    persona.email = (datos.get("email") or "").strip()[:190] or None
    persona.notas = (datos.get("notas") or "").strip() or None
    if "activo" in datos:
        persona.activo = bool(datos.get("activo"))
    persona.updated_at = now
    db.flush()
    return persona


def asegurar(db: Session, nombre: Optional[str], telegram_id: Optional[str]) -> Persona:
    """La persona con ese Telegram, o con ese nombre, o una nueva."""
    p = por_telegram(db, telegram_id) or (por_nombre(db, nombre) if nombre else None)
    if p is None:
        p = Persona(nombre=(nombre or "(sin nombre)")[:120], telegram_id=telegram_id or None,
                    activo=True, created_at=datetime.now(), updated_at=datetime.now())
        db.add(p)
        db.flush()
    elif telegram_id and not p.telegram_id:
        p.telegram_id = telegram_id
    return p


def sincronizar(db: Session) -> int:
    """Enlaza con una persona cualquier preferencia de módulo que no la tenga.

    Pasa si el código viejo (antes del despliegue del directorio) creó un
    destinatario o una persona de mantenimiento: se llama al arrancar la app y
    es idempotente. Devuelve cuántas filas enlazó.
    """
    from app.models.water_quality_alerts import WaterQualityAlertRecipient
    from app.maintenance.models import MntPersona

    n = 0
    for r in db.query(WaterQualityAlertRecipient).filter(WaterQualityAlertRecipient.persona_id.is_(None)).all():
        r.persona_id = asegurar(db, r._name, r._telegram_chat_id).id
        n += 1
    for m in db.query(MntPersona).filter(MntPersona.persona_id.is_(None)).all():
        m.persona_id = asegurar(db, m._nombre, m._telegram_user_id).id
        n += 1
    return n


def participacion(db: Session) -> dict:
    """persona_id → {"wq": destinatario de calidad de agua, "mnt": rol de mantenimiento}."""
    from app.models.water_quality_alerts import WaterQualityAlertRecipient
    from app.maintenance.models import MntPersona

    out: dict = {}
    for r in db.query(WaterQualityAlertRecipient).filter(WaterQualityAlertRecipient.persona_id.isnot(None)).all():
        out.setdefault(r.persona_id, {})["wq"] = r
    for m in db.query(MntPersona).filter(MntPersona.persona_id.isnot(None)).all():
        out.setdefault(m.persona_id, {})["mnt"] = m
    return out
