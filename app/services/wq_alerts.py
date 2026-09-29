"""Motor de alertas de calidad de agua: de "el panel lo muestra" a "hay una fila".

El módulo ya sabía DECIDIR que algo está mal; lo que faltaba era que esa
decisión sobreviviera a que nadie tenga la pantalla abierta. Acá la condición
se convierte en una fila de `water_quality_alerts` con apertura, reconocimiento
y cierre, que es lo que después se puede notificar y auditar.

Tres detecciones, y las tres son **idempotentes**: correr el motor diez veces
seguidas deja la misma fila tocada diez veces, no diez filas. Eso es lo que
permite llamarlo desde donde sea —después de cada ingreso y además cada pocos
minutos— sin coordinar nada.

  - `lectura_alarma`  la última lectura de un estanque está fuera de rango.
  - `ronda_vencida`   nadie midió en la unidad dentro del plazo del turno.
  - `sin_reconocer`   una alarma lleva demasiado rato sin que nadie se haga cargo.

**Se cierran solas.** Cada pasada calcula el conjunto de condiciones vivas; las
alertas abiertas de ese tipo que ya no están en el conjunto se cierran como
`resuelta`. Es decir: la lectura siguiente vuelve a rango y la alerta se cierra;
al fin se tomó la ronda y se cierra. Nadie tiene que acordarse de apagarlas.

Acá NO se envía nada. El ingreso del operador sólo abre la fila; el despacho
corre aparte (PR siguiente). Así un POST de la PWA nunca queda esperando a que
Telegram conteste, y todo envío tiene un solo camino reintentable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.cultivation_units import CultivationUnit
from app.models.pond_oxygen_readings import PondOxygenReading
from app.models.ponds import Pond
from app.models.water_quality_alerts import WaterQualityAlert, WaterQualityAlertRule
from app.services import water_quality as wq
from app.services.wq_queries import latest_o2_by_pond, load_thresholds, o2_hours_ago

logger = logging.getLogger("wq_alerts")

KIND_LECTURA = "lectura_alarma"
KIND_RONDA = "ronda_vencida"
KIND_SIN_ACK = "sin_reconocer"

NIVEL = {"ok": 0, "alerta": 1, "alarma": 2}

# Respaldo si la regla no trae el dato (fila borrada a mano, BD a medio migrar).
ESCALATE_MIN_DEFAULT = 30


@dataclass
class Candidata:
    """Una condición viva detectada en esta pasada. Todavía no es una fila."""
    kind: str
    key: str
    level: str
    title: str
    detail: str
    pond_id: Optional[int] = None
    unit_id: Optional[int] = None
    reading_id: Optional[int] = None
    payload: dict = field(default_factory=dict)


def _n(v, dec=1) -> str:
    """Número con coma decimal, como se escribe en la planta."""
    if v is None:
        return "?"
    return ("{:.%df}" % dec).format(float(v)).replace(".", ",")


def _code(name: Optional[str]) -> str:
    """Sólo el código del estanque: "C6", no "C6 - Carriles 6"."""
    return (name or "").split(" - ")[0].strip() or "?"


def load_rules(db: Session) -> dict:
    return {r.kind: r for r in db.query(WaterQualityAlertRule).all()}


# ---------------------------------------------------------------------------
# Los cortes del consolidado
# ---------------------------------------------------------------------------
# A qué hora salen los mensajes de rutina: `digest_cortes` (10:00 y 19:00 por
# defecto), configurable en Alertas → Reglas.
#
# Tiene configuración propia y NO usa `o2_day_window` a propósito, aunque esa
# ventana también parta el día en dos. Son cosas distintas: `o2_day_window` es
# el turno con que se miden las rondas de O₂ —de él salen los plazos de
# vencimiento— y si el corte de los mensajes colgara de ahí, mover la hora a la
# que alguien lee un resumen movería también cuándo se considera vencida una
# ronda. Acá no hay excepción de domingo: el resumen sale a las mismas horas
# todos los días.
CORTES_DEFAULT = {"alert": 10.0, "alarm": 19.0}


def _cortes(thresholds: Optional[dict] = None) -> tuple:
    w = (thresholds or {}).get("digest_cortes") or CORTES_DEFAULT
    lo = w.get("alert")
    hi = w.get("alarm")
    lo = CORTES_DEFAULT["alert"] if lo is None else float(lo)
    hi = CORTES_DEFAULT["alarm"] if hi is None else float(hi)
    return lo, hi


def turno_de(cuando: datetime, thresholds: Optional[dict] = None) -> str:
    """"dia" u "off" según el tramo en que cayó ese momento."""
    lo, hi = _cortes(thresholds)
    h = cuando.hour + cuando.minute / 60.0
    return "dia" if lo <= h < hi else "off"


def _cortes_del_dia(d, thresholds) -> list:
    """Los instantes en que sale el consolidado ese día."""
    lo, hi = _cortes(thresholds)
    return [datetime.combine(d, dtime(int(h), int(round((h % 1) * 60))))
            for h in (lo, hi)]


def turno_inicio(cuando: datetime, thresholds: Optional[dict] = None) -> datetime:
    """Cuándo empezó el tramo vigente: el último corte anterior a `cuando`.

    Retrocede día a día porque antes del primer corte de hoy manda el segundo
    de ayer: a las 07:00 el tramo vigente arrancó ayer a las 19:00, y el
    consolidado de las 10:00 tiene que cubrir toda esa noche.
    """
    d = cuando.date()
    for _ in range(9):
        previos = [c for c in _cortes_del_dia(d, thresholds) if c <= cuando]
        if previos:
            return max(previos)
        d = d - timedelta(days=1)
    return cuando - timedelta(days=1)


# ---------------------------------------------------------------------------
# Detección
# ---------------------------------------------------------------------------
def _scan_lectura_alarma(now, thresholds, latest, ponds) -> list:
    """La última lectura del estanque está en `alerta` o `alarma`.

    La condición es "la ÚLTIMA lectura está fuera de rango", no "hubo una
    lectura fuera de rango": por eso se cierra sola cuando llega una buena, sin
    que nadie la toque.
    """
    out = []
    for p in ponds:
        r = latest.get(p.id)
        if r is None or r.alarm_level not in ("alerta", "alarma"):
            continue
        do = float(r.do_mg_l) if r.do_mg_l is not None else None
        temp = float(r.water_temp_c) if r.water_temp_c is not None else None
        sat = (float(r.saturation_computed_pct)
               if r.saturation_computed_pct is not None
               else (wq.expected_saturation_pct(do, temp)
                     if do is not None and temp is not None else None))
        detalle = "{}: {} mg/L".format(_code(p.name), _n(do, 2))
        if sat is not None:
            detalle += " · {}% sat".format(_n(sat, 0))
        if temp is not None:
            detalle += " · {} °C".format(_n(temp, 1))
        detalle += " ({})".format(r.reading_datetime.strftime("%d-%m %H:%M"))
        if r.consistency_flag == "sospechoso":
            detalle += ". Terna sospechosa: confirmar antes de actuar."
        out.append(Candidata(
            kind=KIND_LECTURA,
            key="{}:pond={}".format(KIND_LECTURA, p.id),
            level=r.alarm_level,
            title="O₂ fuera de rango · {}".format(_code(p.name)),
            detail=detalle,
            pond_id=p.id,
            unit_id=p.cultivation_unit_id,
            reading_id=r.id,
            payload={"do_mg_l": do, "temp_c": temp, "sat_pct": sat,
                     "consistency": r.consistency_flag,
                     "reading_at": r.reading_datetime.isoformat()},
        ))
    return out


def _scan_ronda_vencida(now, thresholds, latest, ponds_by_unit, units,
                        rule=None) -> list:
    """Nadie midió en la unidad dentro del plazo del turno.

    **Por unidad, no por estanque.** Una noche sin medir en una unidad de doce
    estanques serían doce avisos idénticos, y un aviso que se repite doce veces
    deja de leerse. La medida es la lectura MÁS FRESCA de la unidad: si ninguna
    es reciente, la ronda no se hizo.

    El plazo no es parejo — sale de `o2_stale_thresholds`, que lo toma del turno
    en que se tomó la última lectura (4 h de día, 2 h de noche y domingo).

    **Nace `alerta`, no `alarma`**: que falte un dato no es un pez en riesgo, es
    una falta de dato, y en la práctica la ronda se atrasa todos los días en el
    hueco entre el último dato de la noche y el primero del día. Sube a `alarma`
    —y entonces sí suena— cuando el atraso pasa de `notify_after_min` sobre el
    plazo: ahí deja de ser un atraso y es un hueco. Con eso el criterio de qué
    interrumpe puede ser el nivel y nada más, sin excepciones por tipo.

    Una unidad sin ninguna lectura en su historia se omite: no está en el
    programa de rondas, y si no, arrastraría una alerta abierta para siempre.
    """
    escala_min = getattr(rule, "notify_after_min", None)
    out = []
    for u in units:
        u_ponds = ponds_by_unit.get(u.id, [])
        if not u_ponds:
            continue
        rs = [latest.get(p.id) for p in u_ponds]
        rs = [r for r in rs if r is not None]
        if not rs:
            continue
        fresh = max(rs, key=lambda r: r.reading_datetime)
        horas, vencida = o2_hours_ago(fresh, now, thresholds)
        if not vencida:
            continue
        plazo = wq.o2_stale_thresholds(fresh.reading_datetime, thresholds)[1]
        sin_dato = sum(1 for p in u_ponds if latest.get(p.id) is None)
        detalle = ("{}: sin lecturas de O₂ hace {} h (el turno de esa lectura "
                   "daba {} h). Última: {} en {}.").format(
            u.name, _n(horas, 1), _n(plazo, 1),
            fresh.reading_datetime.strftime("%d-%m %H:%M"),
            _code(next((p.name for p in u_ponds if p.id == fresh.pond_id), None)))
        if sin_dato:
            detalle += " {} de {} estanques nunca han tenido lectura.".format(
                sin_dato, len(u_ponds))
        # Minutos de atraso POR SOBRE el plazo: es lo que separa "viene
        # atrasada" de "no se tomó".
        atraso_min = (horas - plazo) * 60.0
        nivel = ("alarma" if escala_min is not None and atraso_min >= escala_min
                 else "alerta")
        if nivel == "alarma":
            detalle += " Lleva {} h sobre el plazo.".format(_n(atraso_min / 60.0, 1))
        out.append(Candidata(
            kind=KIND_RONDA,
            key="{}:unit={}".format(KIND_RONDA, u.id),
            level=nivel,
            title="Ronda de O₂ sin tomar · {}".format(u.name),
            detail=detalle,
            unit_id=u.id,
            reading_id=fresh.id,
            payload={"horas": horas, "plazo_h": plazo,
                     "atraso_min": round(atraso_min),
                     "estanques": len(u_ponds), "sin_dato": sin_dato,
                     "ultima_at": fresh.reading_datetime.isoformat()},
        ))
    return out


def _scan_sin_reconocer(db: Session, now, rules) -> list:
    """Una alarma lleva `escalate_after_min` sin que nadie la reconozca.

    Se apoya en las alertas ya abiertas, no en las lecturas: la escalada es una
    propiedad de la alerta (cuánto lleva abierta), no del dato. Por eso este
    scan corre DESPUÉS de aplicar los otros dos.

    El reconocimiento sigue viviendo en la lectura (`acknowledged`, que marca el
    operador desde el form o la PWA): es el mismo gesto que ya existía, no uno
    nuevo que haya que enseñar.
    """
    rule = rules.get(KIND_SIN_ACK)
    minutos = (rule.escalate_after_min if rule is not None
               and rule.escalate_after_min else ESCALATE_MIN_DEFAULT)
    corte = now - timedelta(minutes=minutos)
    out = []
    q = (db.query(WaterQualityAlert, PondOxygenReading)
           .join(PondOxygenReading,
                 WaterQualityAlert.source_reading_id == PondOxygenReading.id)
           .filter(WaterQualityAlert.kind == KIND_LECTURA,
                   WaterQualityAlert.level == "alarma",
                   WaterQualityAlert.state == "abierta",
                   WaterQualityAlert.closed_at.is_(None),
                   WaterQualityAlert.opened_at <= corte,
                   PondOxygenReading.acknowledged.is_(False)))
    for alerta, r in q.all():
        mins = int((now - alerta.opened_at).total_seconds() // 60)
        out.append(Candidata(
            kind=KIND_SIN_ACK,
            key="{}:alert={}".format(KIND_SIN_ACK, alerta.id),
            level="alarma",
            title="Alarma sin reconocer · {} min".format(mins),
            detail=("{} lleva {} min en alarma y nadie la ha reconocido ni ha "
                    "registrado acción correctiva.").format(alerta.title, mins),
            pond_id=alerta.pond_id,
            unit_id=alerta.cultivation_unit_id,
            reading_id=alerta.source_reading_id,
            payload={"alerta_id": alerta.id, "minutos": mins,
                     "umbral_min": minutos},
        ))
    return out


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
def _touch(a: WaterQualityAlert, c: Candidata, now) -> bool:
    """Actualiza una alerta que sigue viva. Devuelve True si ESCALÓ.

    **El detalle se refresca siempre.** Una alerta abierta describe una
    condición que sigue pasando, así que sus números tienen que ser los de
    ahora: congelado, el aviso de una ronda vencida hace catorce horas decía
    "hace 3,0 h", que fue verdad sólo en el instante en que se abrió. Lo que sí
    queda inmutable es `message_text` en la bitácora de envíos — ahí se guarda
    palabra por palabra lo que se le mandó a alguien, que es donde importa que
    el historial no cambie.

    Escalar (alerta → alarma) es aparte: es un hecho nuevo y no la misma
    noticia, así que reabre la alerta si estaba reconocida y limpia la
    contabilidad de envíos para que vuelva a sonar.
    """
    a.last_seen_at = now
    a.updated_at = now
    a.detail = c.detail
    a.payload = c.payload
    a.source_reading_id = c.reading_id
    if NIVEL.get(c.level, 0) > NIVEL.get(a.level, 0):
        a.level = c.level
        a.title = c.title
        a.state = "abierta"
        a.last_notified_at = None
        return True
    return False


def _open_or_touch(db: Session, c: Candidata, now) -> str:
    """Abre la alerta o toca la que ya estaba. Devuelve 'abierta'|'escalada'|'tocada'.

    El `IntegrityError` no es un caso raro sino el esperado: el índice único
    parcial es lo que resuelve la carrera entre el motor corriendo por el POST
    de un operador y el mismo motor corriendo en el job. El que pierde no
    reintenta a ciegas — relee la fila que ganó y la toca.
    """
    a = (db.query(WaterQualityAlert)
           .filter(WaterQualityAlert.kind == c.kind,
                   WaterQualityAlert.dedupe_key == c.key,
                   WaterQualityAlert.closed_at.is_(None))
           .first())
    if a is not None:
        return "escalada" if _touch(a, c, now) else "tocada"

    nueva = WaterQualityAlert(
        kind=c.kind, dedupe_key=c.key, level=c.level, state="abierta",
        pond_id=c.pond_id, cultivation_unit_id=c.unit_id,
        opened_at=now, last_seen_at=now,
        source_reading_id=c.reading_id,
        title=c.title, detail=c.detail, payload=c.payload,
        notify_count=0, created_at=now, updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(nueva)
            db.flush()
        return "abierta"
    except IntegrityError:
        a = (db.query(WaterQualityAlert)
               .filter(WaterQualityAlert.kind == c.kind,
                       WaterQualityAlert.dedupe_key == c.key,
                       WaterQualityAlert.closed_at.is_(None))
               .first())
        if a is None:          # perdió la carrera y además ya se cerró
            return "tocada"
        return "escalada" if _touch(a, c, now) else "tocada"


def _close_missing(db: Session, kind: str, vivas: set, now) -> int:
    """Cierra las abiertas de ese tipo que ya no están vivas.

    Es el único mecanismo de cierre automático y el que hace que la tabla
    describa el presente: lo que el motor dejó de ver, se resolvió.
    """
    n = 0
    for a in (db.query(WaterQualityAlert)
                .filter(WaterQualityAlert.kind == kind,
                        WaterQualityAlert.closed_at.is_(None))
                .all()):
        if a.dedupe_key in vivas:
            continue
        razon = "resuelta"
        if kind == KIND_SIN_ACK and a.source_reading_id:
            r = db.get(PondOxygenReading, a.source_reading_id)
            if r is not None and r.acknowledged:
                razon = "reconocida"
        a.closed_at = now
        a.state = "cerrada"
        a.close_reason = razon
        a.updated_at = now
        n += 1
    return n


def _propagar_reconocimiento(db: Session, now) -> int:
    """Marca como `reconocida` la alerta cuya lectura el operador ya reconoció.

    No la cierra: el O₂ sigue bajo hasta que una lectura nueva diga lo
    contrario. Lo que cambia es que alguien se hizo cargo, y eso basta para que
    deje de insistir.
    """
    n = 0
    q = (db.query(WaterQualityAlert, PondOxygenReading)
           .join(PondOxygenReading,
                 WaterQualityAlert.source_reading_id == PondOxygenReading.id)
           .filter(WaterQualityAlert.kind == KIND_LECTURA,
                   WaterQualityAlert.state == "abierta",
                   WaterQualityAlert.closed_at.is_(None),
                   PondOxygenReading.acknowledged.is_(True)))
    for a, r in q.all():
        a.state = "reconocida"
        a.acked_at = a.acked_at or now
        a.acked_by = a.acked_by or r.operator_id
        a.ack_note = a.ack_note or r.corrective_action
        a.updated_at = now
        n += 1
    return n


# ---------------------------------------------------------------------------
# Pasada completa
# ---------------------------------------------------------------------------
def run_detection(db: Session, now: Optional[datetime] = None) -> dict:
    """Una pasada del motor. Idempotente y transaccional.

    Devuelve un resumen por tipo, que es lo que el job registra en el log y la
    vista de monitoreo muestra como "última corrida".
    """
    now = now or datetime.now()
    rules = load_rules(db)
    thresholds = load_thresholds(db)

    ponds = (db.query(Pond)
               .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
               .order_by(Pond.name).all())
    units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
    ponds_by_unit: dict = {}
    for p in ponds:
        ponds_by_unit.setdefault(p.cultivation_unit_id, []).append(p)
    latest = latest_o2_by_pond(db)

    res = {"abiertas": 0, "escaladas": 0, "tocadas": 0, "cerradas": 0,
           "reconocidas": 0, "por_tipo": {}}

    def aplicar(kind: str, candidatas: list) -> None:
        rule = rules.get(kind)
        # Apagar un tipo cierra lo que tenía abierto. Si no, quedarían colgadas
        # para siempre: nada las volvería a mirar.
        if rule is not None and not rule.enabled:
            res["cerradas"] += _close_missing(db, kind, set(), now)
            res["por_tipo"][kind] = "apagada"
            return
        vivas = set()
        cuenta = {"abierta": 0, "escalada": 0, "tocada": 0}
        for c in candidatas:
            vivas.add(c.key)
            cuenta[_open_or_touch(db, c, now)] += 1
        cerradas = _close_missing(db, kind, vivas, now)
        res["abiertas"] += cuenta["abierta"]
        res["escaladas"] += cuenta["escalada"]
        res["tocadas"] += cuenta["tocada"]
        res["cerradas"] += cerradas
        res["por_tipo"][kind] = {"vivas": len(vivas), "nuevas": cuenta["abierta"],
                                 "escaladas": cuenta["escalada"],
                                 "cerradas": cerradas}

    aplicar(KIND_LECTURA, _scan_lectura_alarma(now, thresholds, latest, ponds))
    aplicar(KIND_RONDA, _scan_ronda_vencida(now, thresholds, latest,
                                            ponds_by_unit, units,
                                            rules.get(KIND_RONDA)))

    # El reconocimiento y la escalada miran las alertas recién aplicadas, así
    # que van al final y en este orden: primero quién se hizo cargo, después a
    # quién hay que ir a buscar.
    res["reconocidas"] = _propagar_reconocimiento(db, now)
    db.flush()
    aplicar(KIND_SIN_ACK, _scan_sin_reconocer(db, now, rules))

    db.commit()
    return res


def run_detection_safe(db: Session, now: Optional[datetime] = None) -> Optional[dict]:
    """`run_detection` que no puede voltear al que la llamó.

    La usan los handlers de ingreso, donde la lectura del operador YA está
    comprometida. Que falle la detección es un problema del aviso, no del dato:
    si esto explotara hacia arriba, el operador vería un error 500 después de
    haber registrado bien su lectura, y volvería a digitarla.
    """
    try:
        return run_detection(db, now)
    except (SQLAlchemyError, Exception):  # noqa: B014  (amplio a propósito)
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("fallo la deteccion de alertas de calidad de agua")
        return None
