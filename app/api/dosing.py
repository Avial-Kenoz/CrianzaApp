"""API de sincronización de la PWA de dosificación de biofiltros.

Contrato versionado bajo /api/dosing/v1:
  - GET  /bootstrap  → instrucciones vigentes (solo la cantidad instruida)
                       y lo aplicado hoy.
  - POST /doses      → cargas aplicadas, en lote e idempotente por
                       `client_uuid` (reintentar la sync no duplica).

La sugerencia del modelo NO viaja: la tablet muestra lo que instruyó el
supervisor. Ver app/services/dosing.py.

No se elige operador: quien registra en la tablet es «Turno», una persona que
cambia todo el tiempo (decisión del usuario). Las cargas llegan con
`operator_id` NULL y el escritorio las muestra como «Turno» (por `client_uuid`).

El bootstrap marca `received_at` en las instrucciones que descarga por primera
vez: es la única forma que tiene el escritorio de saber si la instrucción ya
llegó a terreno (la tablet solo se entera cuando la app está abierta con red).
"""

from datetime import datetime, date
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionLocal
from app.models.cultivation_units import CultivationUnit
from app.models.dosing_instructions import DosingInstruction
from app.models.water_treatment_doses import WaterTreatmentDose
from app.services import dosing as ds

router = APIRouter(prefix="/api/dosing/v1", tags=["dosificacion"])

MAX_CLIENT_UUID_LEN = 64
MAX_KG = 9999.0          # cabe en Numeric(10,2) con holgura; sobre esto es digitación


@router.get("/bootstrap")
def bootstrap():
    db = SessionLocal()
    try:
        now = datetime.now()
        instructions = ds.active_instructions(db)
        unit_ids = {i.cultivation_unit_id for i in instructions}
        units = {u.id: u.name for u in
                 db.query(CultivationUnit).filter(CultivationUnit.id.in_(unit_ids)).all()}
        applied = ds.applied_on(db, now.date(), unit_ids)

        pending = [i for i in instructions if i.received_at is None]
        for i in pending:
            i.received_at = now
        if pending:
            db.commit()
        return {
            "server_time": now.isoformat(),
            "today": now.date().isoformat(),
            "instructions": [
                {
                    "id": i.id,
                    "unit_id": i.cultivation_unit_id,
                    "unit_name": units.get(i.cultivation_unit_id, f"Unidad {i.cultivation_unit_id}"),
                    "product": i.product,
                    "product_label": ds.PRODUCT_LABELS.get(i.product, i.product),
                    "kg_day": float(i.kg_day),
                    "note": i.note,
                    "issued_at": i.issued_at.isoformat() if i.issued_at else None,
                    "applied_today_kg": applied.get((i.cultivation_unit_id, i.product), 0.0),
                }
                for i in sorted(instructions,
                                key=lambda i: (units.get(i.cultivation_unit_id, ""), i.product))
            ],
        }
    finally:
        db.close()


class DoseIn(BaseModel):
    client_uuid: str
    instruction_id: int
    applied_at: datetime
    kg: float
    observation: Optional[str] = None


class DoseBatchIn(BaseModel):
    doses: List[DoseIn] = Field(default_factory=list)


class ItemResult(BaseModel):
    client_uuid: str
    status: str                      # created | duplicate | error
    id: Optional[int] = None
    error: Optional[str] = None


class BatchResult(BaseModel):
    created: int
    duplicates: int
    errors: int
    results: List[ItemResult]


@router.post("/doses", response_model=BatchResult)
def upload_doses(payload: DoseBatchIn):
    db = SessionLocal()
    try:
        uuids = [d.client_uuid for d in payload.doses]
        existing = {r[0] for r in db.query(WaterTreatmentDose.client_uuid)
                    .filter(WaterTreatmentDose.client_uuid.in_(uuids)).all()}
        results: List[ItemResult] = []
        created = duplicates = errors = 0

        def fail(uuid, msg):
            nonlocal errors
            errors += 1
            results.append(ItemResult(client_uuid=uuid[:MAX_CLIENT_UUID_LEN],
                                      status="error", error=msg))

        for item in payload.doses:
            if not item.client_uuid or len(item.client_uuid) > MAX_CLIENT_UUID_LEN:
                fail(item.client_uuid or "", "client_uuid inválido")
                continue
            if item.client_uuid in existing:
                duplicates += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="duplicate"))
                continue
            if not (0 < item.kg <= MAX_KG):
                fail(item.client_uuid, f"Cantidad fuera de rango: {item.kg}")
                continue
            # La unidad y el producto salen de la instrucción, no del cliente.
            # Si la instrucción ya fue reemplazada se registra igual contra
            # ella: la carga ocurrió y es lo que el operador tenía en pantalla.
            ins = db.query(DosingInstruction).get(item.instruction_id)
            if ins is None:
                fail(item.client_uuid, "Instrucción inexistente")
                continue
            sp = db.begin_nested()
            try:
                d = WaterTreatmentDose(
                    cultivation_unit_id=ins.cultivation_unit_id,
                    applied_at=item.applied_at.replace(tzinfo=None),
                    product=ins.product,
                    kg=round(item.kg, 2),
                    mode="mantencion",
                    application_point="entrada de biofiltro",
                    observation=(item.observation or None) and item.observation[:255],
                    client_uuid=item.client_uuid,
                    instruction_id=ins.id,
                )
                db.add(d)
                db.flush()
                sp.commit()
            except SQLAlchemyError as exc:
                sp.rollback()
                fail(item.client_uuid, f"No se pudo guardar: {exc.__class__.__name__}")
                continue
            existing.add(item.client_uuid)
            created += 1
            results.append(ItemResult(client_uuid=item.client_uuid, status="created", id=d.id))

        db.commit()
        return BatchResult(created=created, duplicates=duplicates, errors=errors,
                           results=results)
    finally:
        db.close()
