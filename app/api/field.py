"""API de sincronización para el cliente de captura en terreno (PWA offline).

Contrato versionado bajo /api/field/v1:
  - GET  /bootstrap        → estanques + unidades + umbrales + sitio (para
                             cachear en el teléfono y validar offline).
  - POST /oxygen-readings  → carga en lote de lecturas de O2. Idempotente por
                             `client_uuid` (reintentar la sync no duplica).

El grueso de la captura (O2, alto volumen) va por acá; el biofiltro (diario,
bajo volumen) sigue con su formulario web.
"""

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from datetime import datetime
from typing import List, Optional

MAX_CLIENT_UUID_LEN = 64

from app.db.session import SessionLocal
from app.models.ponds import Pond
from app.models.cultivation_units import CultivationUnit
from app.models.pond_oxygen_readings import PondOxygenReading
from app.services import water_quality as wq
from app.api.water_quality import (
    load_thresholds, ensure_pond_qr_codes, recent_unit_temp, CORRECTIVE_ACTIONS,
)

router = APIRouter(prefix="/api/field/v1", tags=["field"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Límites de las columnas Numeric de pond_oxygen_readings (precisión, escala).
# do_mg_l/saturación = Numeric(6,2) → |v| ≤ 9999.99 ; temp = Numeric(5,2) → ≤ 999.99.
def _fits_numeric_columns(do_mg_l, water_temp_c, saturation_pct, saturation_computed_pct) -> bool:
    def _ok(v, max_abs):
        return v is None or abs(float(v)) <= max_abs
    return (_ok(do_mg_l, 9999.99) and _ok(water_temp_c, 999.99)
            and _ok(saturation_pct, 9999.99) and _ok(saturation_computed_pct, 9999.99))


# ---------------------------------------------------------------------------
# Bootstrap (bajada)
# ---------------------------------------------------------------------------
@router.get("/bootstrap")
def bootstrap():
    db = SessionLocal()
    try:
        ensure_pond_qr_codes(db)  # garantiza qr_code para mapear escaneos offline
        unit_names = {u.id: u.name for u in db.query(CultivationUnit).order_by(CultivationUnit.name).all()}
        ponds = (
            db.query(Pond)
            .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
            .order_by(Pond.name)
            .all()
        )
        return {
            "server_time": datetime.now().isoformat(),
            "site": {"altitude_m": wq.SITE_ALTITUDE_M},
            "units": [{"id": uid, "name": name} for uid, name in unit_names.items()],
            "ponds": [
                {
                    "id": p.id,
                    "name": p.name,
                    "code": p.code,
                    "qr_code": p.qr_code,
                    "cultivation_unit_id": p.cultivation_unit_id,
                    "cultivation_unit_name": unit_names.get(p.cultivation_unit_id),
                }
                for p in ponds
            ],
            "thresholds": load_thresholds(db),
            "corrective_actions": CORRECTIVE_ACTIONS,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Carga en lote de lecturas de O2 (subida, idempotente)
# ---------------------------------------------------------------------------
class OxygenReadingIn(BaseModel):
    client_uuid: str
    pond_id: int
    reading_datetime: datetime
    do_mg_l: Optional[float] = None
    water_temp_c: Optional[float] = None
    saturation_pct: Optional[float] = None
    operator_id: Optional[int] = None
    observation: Optional[str] = None
    # Llamado a la acción ante alarma: el operador reconoció la lectura y anotó
    # la acción correctiva (solo se persisten si la lectura resulta en alarma).
    acknowledged: Optional[bool] = False
    corrective_action: Optional[str] = None


class OxygenBatchIn(BaseModel):
    readings: List[OxygenReadingIn]


class ItemResult(BaseModel):
    client_uuid: str
    status: str  # created | duplicate | error
    id: Optional[int] = None
    alarm_level: Optional[str] = None
    consistency_flag: Optional[str] = None
    error: Optional[str] = None


class BatchResult(BaseModel):
    created: int
    duplicates: int
    errors: int
    results: List[ItemResult]


@router.post("/oxygen-readings", response_model=BatchResult)
def upload_oxygen_readings(payload: OxygenBatchIn):
    db = SessionLocal()
    try:
        items = payload.readings
        uuids = [i.client_uuid for i in items]
        pond_ids = {i.pond_id for i in items}

        # Idempotencia: client_uuid ya presentes en la BD
        existing = {
            r.client_uuid
            for r in db.query(PondOxygenReading.client_uuid)
            .filter(PondOxygenReading.client_uuid.in_(uuids))
            .all()
        }
        pond_unit = {
            pid: uid
            for (pid, uid) in db.query(Pond.id, Pond.cultivation_unit_id)
            .filter(Pond.id.in_(pond_ids)).all()
        }
        valid_ponds = set(pond_unit)
        thresholds = load_thresholds(db)

        # Temperatura de la ronda por unidad, tomada del propio lote: la primera
        # lectura (cronológica) con temperatura medida de cada unidad. Los
        # estanques que la omiten la heredan (laguna homogénea).
        batch_temp_by_unit: dict = {}
        for it in sorted(items, key=lambda x: x.reading_datetime):
            uid = pond_unit.get(it.pond_id)
            if uid is not None and it.water_temp_c is not None:
                batch_temp_by_unit.setdefault(uid, float(it.water_temp_c))

        results: List[ItemResult] = []
        created = duplicates = errors = 0

        for item in items:
            if not item.client_uuid or len(item.client_uuid) > MAX_CLIENT_UUID_LEN:
                errors += 1
                results.append(ItemResult(client_uuid=item.client_uuid[:MAX_CLIENT_UUID_LEN],
                                          status="error", error="client_uuid inválido"))
                continue
            if item.client_uuid in existing:
                duplicates += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="duplicate"))
                continue
            if item.pond_id not in valid_ponds:
                errors += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="error",
                                          error="pond_id inexistente o inactivo"))
                continue

            # Herencia de temperatura si la lectura la omite: primero la del
            # lote para esa unidad, luego la última medida reciente en la BD.
            temp = item.water_temp_c
            temp_inherited = False
            if temp is None:
                uid = pond_unit.get(item.pond_id)
                temp = batch_temp_by_unit.get(uid)
                if temp is None:
                    temp = recent_unit_temp(db, uid, item.reading_datetime)
                temp_inherited = temp is not None

            res = wq.evaluate_oxygen(item.do_mg_l, temp, item.saturation_pct,
                                     thresholds=thresholds)

            # Defensa anti-desborde: si algún valor no cabe en su columna Numeric
            # (glitch de sensor: O2/temp/saturación absurdos), la lectura se OMITE
            # como error individual sin tocar la BD. Así un dato malo no aborta la
            # transacción ni frena la sincronización del resto del lote.
            if not _fits_numeric_columns(item.do_mg_l, temp, item.saturation_pct,
                                         res.saturation_computed_pct):
                errors += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="error",
                                          error="valores fuera de rango de almacenamiento (lectura omitida)"))
                continue

            reading = PondOxygenReading(
                pond_id=item.pond_id,
                operator_id=item.operator_id,
                reading_datetime=item.reading_datetime,
                do_mg_l=item.do_mg_l,
                water_temp_c=temp,
                water_temp_inherited=temp_inherited,
                saturation_pct=item.saturation_pct,
                saturation_computed_pct=res.saturation_computed_pct,
                consistency_flag=res.consistency_flag,
                alarm_level=res.alarm_level,
                acknowledged=(res.alarm_level == "alarma" and bool(item.acknowledged)),
                corrective_action=(item.corrective_action if res.alarm_level == "alarma" else None),
                observation=(item.observation or None),
                client_uuid=item.client_uuid,
                created_at=datetime.now(),
            )
            try:
                with db.begin_nested():
                    db.add(reading)
                    db.flush()
                existing.add(item.client_uuid)  # evita duplicar dentro del mismo lote
                created += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="created",
                                          id=reading.id, alarm_level=res.alarm_level,
                                          consistency_flag=res.consistency_flag))
            except IntegrityError:
                duplicates += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="duplicate"))
            except SQLAlchemyError:
                errors += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="error",
                                          error="no se pudo guardar la lectura"))

        db.commit()
        return BatchResult(created=created, duplicates=duplicates, errors=errors, results=results)
    finally:
        db.close()
