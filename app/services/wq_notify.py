"""Despacho de las alertas por Telegram: qué sale, a quién y cuándo.

Dos caminos, y la diferencia entre ellos es el **nivel**, no el tipo:

  - **Instantáneo** — lo que llega a `instant_level` (por defecto `alarma`)
    interrumpe en el momento.
  - **Consolidado** — lo que queda entre `min_level` e `instant_level` no le
    suena a nadie: espera al corte del consolidado (`digest_cortes`: 10:00 y
    19:00) y sale junto con lo demás, en un solo mensaje.

Dos cosas que hacen la diferencia entre un canal que se lee y uno que se
silencia, y que no son configuración sino forma de mandar:

1. **Se agrupa por destinatario en cada pasada.** Si en el mismo minuto hay seis
   alertas para la misma persona, sale UN mensaje con seis líneas. El domingo
   que nadie midió, las unidades vencen casi juntas: la diferencia entre un
   mensaje y nueve es exactamente esto.
2. **El silencio es información.** Si en el turno no hubo nada que contar, no se
   manda un "sin novedad": un canal que habla cuando no pasa nada enseña a no
   mirarlo.

El envío corre por su cuenta y nunca cuelga del POST de un operador: el ingreso
sólo abre la alerta en la BD (ver `wq_alerts`) y de acá para adelante es todo
del job. Por eso un fallo de red no pierde nada — la alerta sigue abierta y sin
`last_notified_at`, así que la pasada siguiente la toma.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.cultivation_units import CultivationUnit
from app.models.ponds import Pond
from app.models.water_quality_alerts import (
    WaterQualityAlert,
    WaterQualityAlertNotification,
    WaterQualityAlertRecipient,
    WaterQualityOncallWeek,
)
from app.services import notify_telegram as tg
from app.services import wq_alerts as al
from app.services.wq_queries import load_thresholds

logger = logging.getLogger("wq_notify")

ICONO = {"alarma": "🔴", "alerta": "🟡"}
TURNO_LABEL = {"dia": "turno de día", "off": "turno de noche"}

# Para el enlace del mensaje. El servidor tiene dos IP fijas — 192.168.1.202 en
# Ethernet y 192.168.1.201 en Wi-Fi — y la que responde es la de **Wi-Fi**: la
# de Ethernet no contesta en el puerto 8002. Vale la pena dejarlo escrito
# porque el certificado de Caddy dice ".202" y hace pensar lo contrario. Un
# enlace a la IP equivocada no falla a la vista: manda un mensaje correcto con
# un link que no abre, y eso se descubre de noche.
BASE_URL = (os.getenv("CRIANZA_BASE_URL") or "http://192.168.1.201:8002").rstrip("/")
URL_ALERTAS = BASE_URL + "/views/ui/calidad-agua/alertas"


# ---------------------------------------------------------------------------
# A quién le toca
# ---------------------------------------------------------------------------
def _lista(campo: Optional[str]) -> list:
    return [s.strip() for s in (campo or "").split(",") if s.strip()]


def _dentro_de_ventana(h: float, desde, hasta) -> bool:
    """Ventana horaria que puede cruzar la medianoche (22 a 7 es válida)."""
    lo, hi = float(desde), float(hasta)
    return (lo <= h < hi) if lo <= hi else (h >= lo or h < hi)


def semanero_de(db: Session, cuando: datetime) -> Optional[int]:
    """Quién está de turno esa semana, o None si no hay nadie asignado.

    Devuelve None también cuando el asignado ya no está activo o salió de la
    rotación. Es deliberado: sin ese respaldo, desactivar a una persona dejaría
    al centro mudo hasta que alguien se acordara de reasignar la semana, y la
    única falla que este módulo no puede permitirse es que nadie esté mirando.
    """
    lunes = (cuando.date() - timedelta(days=cuando.weekday()))
    fila = (db.query(WaterQualityOncallWeek)
              .filter(WaterQualityOncallWeek.week_start == lunes).first())
    if fila is None or fila.recipient_id is None:
        return None
    d = db.get(WaterQualityAlertRecipient, fila.recipient_id)
    if d is None or not d.active or not d.in_rotation:
        return None
    return fila.recipient_id


def _recibe(d: WaterQualityAlertRecipient, a: WaterQualityAlert,
            now: datetime, semanero_id: Optional[int] = None,
            consolidado: bool = False) -> bool:
    """Filtros del destinatario. Nulo siempre significa 'sin restricción'.

    El turno manda sobre todo lo demás:

      - semanero de la semana       → todo, y a cualquier hora
      - en rotación, no es semanero → nada: esa semana no le toca
      - semana sin asignar          → todos los de la rotación
      - fuera de rotación           → sólo alarmas, nunca los consolidados
    """
    if not d.active:
        return False
    es_semanero = semanero_id is not None and d.id == semanero_id
    if d.in_rotation:
        if semanero_id is not None and not es_semanero:
            return False
    elif consolidado:
        return False
    if al.NIVEL.get(a.level, 0) < al.NIVEL.get(d.min_level, 2):
        return False
    kinds = _lista(d.kinds)
    if kinds and a.kind not in kinds:
        return False
    unidades = _lista(d.unit_ids)
    if unidades and str(a.cultivation_unit_id or "") not in unidades:
        return False
    dias = _lista(d.weekdays)
    if dias and str(now.weekday()) not in dias:
        return False
    # Al semanero no se le aplica la ventana horaria: estar de turno es
    # precisamente estar disponible a cualquier hora, y si su ventana lo
    # dejara fuera de la noche no habría nadie cubriéndola.
    if (not es_semanero and d.hours_from is not None and d.hours_to is not None):
        if not _dentro_de_ventana(now.hour + now.minute / 60.0,
                                  d.hours_from, d.hours_to):
            return False
    return True


def _silenciado(rule, a: WaterQualityAlert, now: datetime) -> bool:
    """Ventana de silencio de la regla: adentro sólo pasa `alarma`.

    No es un silencio total a propósito. De noche es cuando más caro sale no
    enterarse; lo que se calla son las alertas menores, que pueden esperar.
    """
    if rule is None or rule.quiet_from is None or rule.quiet_to is None:
        return False
    if al.NIVEL.get(a.level, 0) >= al.NIVEL["alarma"]:
        return False
    return _dentro_de_ventana(now.hour + now.minute / 60.0,
                              rule.quiet_from, rule.quiet_to)


def _motivo(a: WaterQualityAlert, rule, now: datetime) -> Optional[str]:
    """Por qué correspondería avisar ahora, o None si no corresponde.

    `last_notified_at` en nulo con `notify_count` ya positivo significa que la
    alerta escaló: `_touch` limpia la contabilidad justamente para que vuelva a
    sonar, porque empeorar es una noticia nueva y no la misma de antes.
    """
    if a.last_notified_at is None:
        return "escalamiento" if (a.notify_count or 0) > 0 else "apertura"
    minutos = (now - a.last_notified_at).total_seconds() / 60.0
    if rule is not None and rule.cooldown_min and minutos < rule.cooldown_min:
        return None
    if rule is not None and rule.renotify_min and minutos >= rule.renotify_min:
        return "recordatorio"
    return None


# ---------------------------------------------------------------------------
# Textos
# ---------------------------------------------------------------------------
def _lugar(a, ponds: dict, units: dict) -> str:
    if a.pond_id and a.pond_id in ponds:
        return ponds[a.pond_id]
    if a.cultivation_unit_id and a.cultivation_unit_id in units:
        return units[a.cultivation_unit_id]
    return ""


def _linea(a) -> str:
    return "{} {}\n   {}".format(ICONO.get(a.level, "•"), a.title, a.detail or "")


def _texto_instantaneo(alertas: list, motivos: dict) -> str:
    cab = "Calidad de agua · {}".format(
        "atención inmediata" if len(alertas) == 1 else
        "{} alertas requieren atención".format(len(alertas)))
    cuerpo = "\n\n".join(_linea(a) for a in alertas)
    recordatorios = sum(1 for a in alertas if motivos.get(a.id) == "recordatorio")
    pie = URL_ALERTAS
    if recordatorios:
        pie = "{} sigue(n) sin resolverse desde el aviso anterior.\n{}".format(
            recordatorios, pie)
    return "{}\n\n{}\n\n{}".format(cab, cuerpo, pie)


def _texto_consolidado(alertas: list, ponds, units, turno: str,
                       desde: datetime, hasta: datetime) -> str:
    por_tipo: dict = {}
    for a in alertas:
        por_tipo.setdefault(a.kind, []).append(a)
    bloques = []
    for kind, grupo in por_tipo.items():
        etiqueta = {al.KIND_LECTURA: "O₂ fuera de rango",
                    al.KIND_RONDA: "Rondas sin tomar",
                    al.KIND_SIN_ACK: "Alarmas sin reconocer"}.get(kind, kind)
        filas = []
        for a in sorted(grupo, key=lambda x: x.opened_at):
            estado = "sigue abierta" if a.closed_at is None else "resuelta"
            filas.append("• {} · {} · {}".format(
                _lugar(a, ponds, units) or a.title,
                a.opened_at.strftime("%H:%M"), estado))
        bloques.append("{} ({})\n{}".format(etiqueta, len(grupo), "\n".join(filas)))

    abiertas = sum(1 for a in alertas if a.closed_at is None)
    cierre = ("Todo se resolvió solo." if not abiertas
              else "{} sigue(n) abierta(s).".format(abiertas))
    return "Resumen del {} · {} → {}\n\n{}\n\n{}\n{}".format(
        TURNO_LABEL.get(turno, turno),
        desde.strftime("%d-%m %H:%M"), hasta.strftime("%H:%M"),
        "\n\n".join(bloques), cierre, URL_ALERTAS)


# ---------------------------------------------------------------------------
# Envío
# ---------------------------------------------------------------------------
def _registrar(db: Session, a, d, texto: str, motivo: str,
               ok: bool, mid, err, now) -> None:
    db.add(WaterQualityAlertNotification(
        alert_id=a.id, recipient_id=d.id, channel="telegram",
        address=d.telegram_chat_id, reason=motivo, sent_at=now,
        ok=ok, error=err, message_text=texto[:1000],
        telegram_message_id=mid, created_at=now))


def _mandar_lote(db: Session, d, alertas: list, texto: str, motivo: str,
                 now: datetime) -> bool:
    """Un mensaje, una fila de bitácora por alerta incluida.

    Las filas comparten `telegram_message_id`, así que después se puede
    reconstruir qué alertas viajaron juntas en el mismo mensaje.
    """
    ok, mid, err = tg.enviar(d.telegram_chat_id, texto)
    for a in alertas:
        _registrar(db, a, d, texto, motivo, ok, mid, err, now)
    if not ok:
        logger.warning("Telegram rechazó el aviso a %s: %s", d.name, err)
    return ok


def dispatch_instant(db: Session, now: Optional[datetime] = None) -> dict:
    """Manda lo que interrumpe, agrupado por destinatario."""
    now = now or datetime.now()
    rules = al.load_rules(db)
    destinatarios = (db.query(WaterQualityAlertRecipient)
                       .filter(WaterQualityAlertRecipient.active.is_(True)).all())
    res = {"mensajes": 0, "alertas": 0, "fallidos": 0}
    if not destinatarios:
        return res

    ponds = {p.id: p.name.split(" - ")[0].strip()
             for p in db.query(Pond.id, Pond.name).all()}
    units = {u.id: u.name for u in db.query(CultivationUnit.id,
                                            CultivationUnit.name).all()}

    candidatas, motivos = [], {}
    for a in (db.query(WaterQualityAlert)
                .filter(WaterQualityAlert.closed_at.is_(None))
                .order_by(WaterQualityAlert.opened_at).all()):
        r = rules.get(a.kind)
        if r is not None and not r.enabled:
            continue
        if r is not None and al.NIVEL.get(a.level, 0) < al.NIVEL.get(r.min_level, 2):
            continue
        # Lo que no llega a interrumpir viaja en el consolidado del turno.
        instante = al.NIVEL.get(r.instant_level, 2) if r is not None else 2
        if al.NIVEL.get(a.level, 0) < instante:
            continue
        # Una alerta reconocida ya tiene a alguien encima: deja de insistir.
        if a.state == "reconocida":
            continue
        if _silenciado(r, a, now):
            continue
        motivo = _motivo(a, r, now)
        if motivo is None:
            continue
        candidatas.append(a)
        motivos[a.id] = motivo

    if not candidatas:
        return res

    semanero = semanero_de(db, now)
    enviado_alguna = False
    for d in destinatarios:
        suyas = [a for a in candidatas if _recibe(d, a, now, semanero)]
        if not suyas:
            continue
        texto = _texto_instantaneo(suyas, motivos)
        motivo_lote = ("recordatorio"
                       if all(motivos[a.id] == "recordatorio" for a in suyas)
                       else "apertura")
        if _mandar_lote(db, d, suyas, texto, motivo_lote, now):
            enviado_alguna = True
            res["mensajes"] += 1
        else:
            res["fallidos"] += 1

    # La marca de tiempo se pone haya salido o no: si falló, el cooldown evita
    # reintentar cada cinco minutos y llenar la bitácora de errores iguales. El
    # contador, en cambio, sólo sube cuando de verdad salió, así que
    # `notify_count` sigue contando avisos entregados y no intentos.
    for a in candidatas:
        a.last_notified_at = now
        a.updated_at = now
        if enviado_alguna:
            a.notify_count = (a.notify_count or 0) + 1
    res["alertas"] = len(candidatas)
    db.commit()
    return res


def dispatch_digest(db: Session, now: Optional[datetime] = None) -> dict:
    """Manda el consolidado del turno que acaba de cerrar.

    No necesita correr a una hora exacta ni recordar si ya salió: toma lo que
    quedó sin avisar **del turno que acaba de cerrar**, así que sale solo en la
    primera pasada después del cambio y no se repite, porque lo que ya salió
    tiene `last_notified_at`.

    La ventana es exactamente ese turno y no "todo lo pendiente" a propósito. Si
    la app estuvo caída un turno entero, ese resumen se pierde — y está bien: un
    resumen del turno de anteayer no lo va a leer nadie, y sin la ventana la
    primera corrida habría mandado de golpe los noventa registros que dejó la
    calibración.
    """
    now = now or datetime.now()
    thresholds = load_thresholds(db)
    inicio = al.turno_inicio(now, thresholds)      # arranque del turno vigente
    # El turno que se resume es el que terminó en `inicio`: se lo ubica un
    # segundo antes de ese corte.
    justo_antes = inicio - timedelta(seconds=1)
    arranque = al.turno_inicio(justo_antes, thresholds)
    rules = al.load_rules(db)
    res = {"mensajes": 0, "alertas": 0, "fallidos": 0}

    destinatarios = (db.query(WaterQualityAlertRecipient)
                       .filter(WaterQualityAlertRecipient.active.is_(True)).all())
    if not destinatarios:
        return res

    pendientes = []
    for a in (db.query(WaterQualityAlert)
                .filter(WaterQualityAlert.last_notified_at.is_(None),
                        WaterQualityAlert.opened_at >= arranque,
                        WaterQualityAlert.opened_at < inicio)
                .order_by(WaterQualityAlert.opened_at).all()):
        r = rules.get(a.kind)
        if r is not None and not r.enabled:
            continue
        if r is not None and al.NIVEL.get(a.level, 0) < al.NIVEL.get(r.min_level, 2):
            continue
        instante = al.NIVEL.get(r.instant_level, 2) if r is not None else 2
        if al.NIVEL.get(a.level, 0) >= instante:
            continue            # ésa va (o fue) por el camino instantáneo
        pendientes.append(a)

    if not pendientes:
        return res              # el silencio es información: no se manda nada

    ponds = {p.id: p.name.split(" - ")[0].strip()
             for p in db.query(Pond.id, Pond.name).all()}
    units = {u.id: u.name for u in db.query(CultivationUnit.id,
                                            CultivationUnit.name).all()}
    turno_cerrado = al.turno_de(justo_antes, thresholds)
    texto_base = _texto_consolidado(pendientes, ponds, units, turno_cerrado,
                                    arranque, inicio)

    semanero = semanero_de(db, now)
    enviado_alguna = False
    for d in destinatarios:
        suyas = [a for a in pendientes if _recibe(d, a, now, semanero,
                                                  consolidado=True)]
        if not suyas:
            continue
        texto = (texto_base if len(suyas) == len(pendientes)
                 else _texto_consolidado(suyas, ponds, units, turno_cerrado,
                                         arranque, inicio))
        if _mandar_lote(db, d, suyas, texto, "resumen", now):
            enviado_alguna = True
            res["mensajes"] += 1
        else:
            res["fallidos"] += 1

    for a in pendientes:
        a.last_notified_at = now
        a.updated_at = now
        if enviado_alguna:
            a.notify_count = (a.notify_count or 0) + 1
    res["alertas"] = len(pendientes)
    db.commit()
    return res


def dispatch(db: Session, now: Optional[datetime] = None) -> dict:
    """Las dos vías, en orden. Devuelve el resumen combinado."""
    now = now or datetime.now()
    if not tg.configurado():
        logger.warning("TELEGRAM_BOT_TOKEN no está en el entorno: no se envía nada")
        return {"sin_token": True}
    inst = dispatch_instant(db, now)
    dig = dispatch_digest(db, now)
    return {"instantaneo": inst, "consolidado": dig}
