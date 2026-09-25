"""Consultas de calidad de agua compartidas entre el router y el motor de alertas.

Vivían en `app/api/water_quality.py` y las usaba sólo el panel. Ahora también
las necesita `app/services/wq_alerts.py`, y un servicio no puede importar un
router sin cerrar el ciclo (el router importa el servicio). Por eso bajan una
capa: el router las sigue exponiendo con los mismos nombres, así que ningún
llamador de fuera cambia.

Acá va lo que toca la BD; el motor puro (fórmulas, umbrales, diagnósticos)
sigue en `app.services.water_quality`, sin sesión ni modelos.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.pond_oxygen_readings import PondOxygenReading
from app.models.water_quality_thresholds import WaterQualityThreshold
from app.services import water_quality as wq


def load_thresholds(db: Session) -> dict:
    """Lee los umbrales activos de la BD con la forma que espera el motor.

    Rellena con DEFAULT_THRESHOLDS cualquier parámetro faltante/inactivo.
    """
    out: dict[str, dict] = {}
    for r in db.query(WaterQualityThreshold).filter(WaterQualityThreshold.active.is_(True)).all():
        out[r.parameter] = {
            "alert": float(r.alert_value) if r.alert_value is not None else None,
            "alarm": float(r.alarm_value) if r.alarm_value is not None else None,
            "comparator": r.comparator,
        }
    for key, spec in wq.DEFAULT_THRESHOLDS.items():
        out.setdefault(key, spec)
    return out


def latest_o2_by_pond(db: Session) -> dict:
    """Última lectura CON OXIGENO por estanque (pond_id -> PondOxygenReading).

    La tabla tambien admite filas de solo pH: desde el 24-09 el pH por
    estanque se mide en rondas propias, a las 08:30 y a las 12:00, sin OD.
    Sin el filtro, una de esas filas se hace pasar por la ultima lectura de O2
    y el panel muestra el estanque en blanco cuando en realidad tiene un dato
    de oxigeno reciente.
    """
    sub = (
        db.query(
            PondOxygenReading.pond_id.label("pid"),
            func.max(PondOxygenReading.reading_datetime).label("mx"),
        )
        .filter(PondOxygenReading.do_mg_l.isnot(None))
        .group_by(PondOxygenReading.pond_id)
        .subquery()
    )
    out = {}
    for r in db.query(PondOxygenReading).filter(
        PondOxygenReading.do_mg_l.isnot(None),
    ).join(
        sub, and_(PondOxygenReading.pond_id == sub.c.pid,
                  PondOxygenReading.reading_datetime == sub.c.mx),
    ).order_by(PondOxygenReading.id.desc()).all():
        out.setdefault(r.pond_id, r)
    return out


def o2_hours_ago(reading, now: datetime, thresholds: Optional[dict] = None):
    """(horas de antiguedad, vencida) segun el turno vigente.

    No es un umbral parejo: la ronda es cada 4 h en el turno de dia de lunes a
    sabado y cada 2 h el resto del tiempo, asi que "vencida" significa que se
    salto una lectura programada, no que pasaron 4 horas.
    """
    if reading is None or reading.reading_datetime is None:
        return None, False
    hours = (now - reading.reading_datetime).total_seconds() / 3600.0
    return round(hours, 1), hours > wq.o2_stale_thresholds(
        reading.reading_datetime, thresholds)[1]
