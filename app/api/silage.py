"""API de sincronización del módulo de molienda y ensilaje (registro PC 03.2).

Contrato versionado bajo /api/silage/v1:
  - GET  /bootstrap              → tambores, umbrales, operadores, últimas cargas
                                   (lo que la PWA cachea para operar sin señal).
  - POST /grinding-events        → carga en lote de molienda. Idempotente por
                                   `client_uuid` (reintentar la sync no duplica).
  - POST /grinding-events/{id}/void     → anular una carga mal registrada.
  - POST /grinding-events/{id}/receive  → recepción por un segundo usuario.
  - POST /weekly-inspections     → inspección semanal (upsert por semana).

Dos cosas que este router resuelve y conviene no perder de vista:

**El folio lo asigna el servidor**, tomándolo de la secuencia `silage_folio_seq`
en el INSERT. La PWA no puede generarlo offline sin arriesgar correlativos
colisionantes entre tablets, así que viaja NULL y vuelve en la respuesta.

**El tambor se abre de forma perezosa.** Al cerrar un tambor NO se abre otro
automáticamente: el siguiente se abre cuando llega la primera carga que lo
referencia. Así nunca existen dos tambores abiertos a la vez (lo que el índice
único parcial `uniq_silage_drum_open` rechazaría de plano) y el operador no
queda atado a un tambor que el sistema eligió por él.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import List, Optional

from app.db.session import SessionLocal
from app.models.users import User
from app.models.silage_drums import SilageDrum
from app.models.silage_grinding_events import SilageGrindingEvent
from app.models.silage_weekly_inspections import SilageWeeklyInspection
from app.models.silage_acid import SilageAcidLot, SilageAcidMovement
from app.models.silage_thresholds import SilageThreshold
from app.services import silage as sg

router = APIRouter(prefix="/api/silage/v1", tags=["ensilaje"])

MAX_CLIENT_UUID_LEN = 64
BOOTSTRAP_EVENT_LIMIT = 30


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def load_thresholds(db) -> dict:
    """Umbrales activos de la BD, rellenando con los defaults del motor lo que
    falte o esté inactivo."""
    out: dict = {}
    for row in db.query(SilageThreshold).filter(SilageThreshold.active.is_(True)).all():
        if row.value is not None:
            out[row.parameter] = float(row.value)
    for key, value in sg.DEFAULT_THRESHOLDS.items():
        out.setdefault(key, value)
    return out


def acid_available(db) -> Decimal:
    """Disponible de ácido calculado desde los movimientos (§ inventario)."""
    movements = db.query(SilageAcidMovement.movement_type, SilageAcidMovement.lts).all()
    return sg.acid_available_lts([{"movement_type": t, "lts": l} for t, l in movements])


def _serialize_event(event: SilageGrindingEvent) -> dict:
    return {
        "id": event.id,
        "folio": event.folio,
        "folio_display": sg.format_folio(event.folio),
        "event_datetime": event.event_datetime.isoformat() if event.event_datetime else None,
        "log_date": event.log_date.isoformat() if event.log_date else None,
        "had_mortality": event.had_mortality,
        "mortality_kg": float(event.mortality_kg) if event.mortality_kg is not None else None,
        "acid_used": event.acid_used,
        "acid_lts": float(event.acid_lts) if event.acid_lts is not None else None,
        "additional_acid": event.additional_acid,
        "ph_value": float(event.ph_value) if event.ph_value is not None else None,
        "ph_below_limit": event.ph_below_limit,
        "drum_id": event.drum_id,
        "drum_number": event.drum_number,
        "drum_closed": event.drum_closed,
        "operator_id": event.operator_id,
        "received_by_name": event.received_by_name,
        "received_at": event.received_at.isoformat() if event.received_at else None,
        "observation": event.observation,
        "voided_at": event.voided_at.isoformat() if event.voided_at else None,
        "void_reason": event.void_reason,
        "client_uuid": event.client_uuid,
    }


# ---------------------------------------------------------------------------
# Bootstrap (bajada)
# ---------------------------------------------------------------------------
@router.get("/bootstrap")
def bootstrap():
    db = SessionLocal()
    try:
        thresholds = load_thresholds(db)
        open_drum = db.query(SilageDrum).filter(SilageDrum.status == "open").first()
        available = (
            db.query(SilageDrum)
            .filter(SilageDrum.status == "available")
            .order_by(SilageDrum.drum_number)
            .all()
        )
        operators = (
            db.query(User)
            .filter(User.active.is_(True))
            .order_by(User.name, User.lastname)
            .all()
        )
        recent = (
            db.query(SilageGrindingEvent)
            .order_by(SilageGrindingEvent.event_datetime.desc())
            .limit(BOOTSTRAP_EVENT_LIMIT)
            .all()
        )
        lots = (
            db.query(SilageAcidLot)
            .filter(SilageAcidLot.active.is_(True))
            .order_by(SilageAcidLot.lot_code)
            .all()
        )

        drum_fill, _ = sg.evaluate_drum_fill(
            open_drum.total_kg if open_drum else 0, thresholds
        )
        return {
            "server_time": datetime.now().isoformat(),
            "thresholds": thresholds,
            "open_drum": (
                {
                    "id": open_drum.id,
                    "drum_number": open_drum.drum_number,
                    "total_kg": float(open_drum.total_kg or 0),
                    "fill_status": drum_fill,
                }
                if open_drum
                else None
            ),
            "available_drums": [
                {"id": d.id, "drum_number": d.drum_number} for d in available
            ],
            "operators": [
                {
                    "id": u.id,
                    "name": " ".join(x for x in [u.name, u.lastname] if x) or u.email,
                }
                for u in operators
            ],
            "acid_lots": [
                {"id": l.id, "lot_code": l.lot_code} for l in lots
            ],
            "acid_available_lts": float(acid_available(db)),
            "recent_events": [_serialize_event(e) for e in recent],
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Carga en lote de cargas de molienda (subida, idempotente)
# ---------------------------------------------------------------------------
class GrindingEventIn(BaseModel):
    client_uuid: str
    event_datetime: datetime
    log_date: Optional[date] = None
    had_mortality: bool = True
    mortality_kg: Optional[float] = None
    acid_used: bool = False
    acid_lts: Optional[float] = None
    additional_acid: bool = False
    ph_value: Optional[float] = None
    # El operador lee el N° rotulado en el tambor físico; el servidor resuelve
    # (o abre) el tambor que corresponde.
    drum_number: Optional[int] = None
    drum_closed: bool = False
    operator_id: Optional[int] = None
    acid_lot_id: Optional[int] = None
    observation: Optional[str] = None


class GrindingBatchIn(BaseModel):
    events: List[GrindingEventIn] = Field(default_factory=list)


class ItemResult(BaseModel):
    client_uuid: str
    status: str                      # created | duplicate | error
    id: Optional[int] = None
    folio: Optional[int] = None
    folio_display: Optional[str] = None
    conformity: Optional[str] = None
    drum_status: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class BatchResult(BaseModel):
    created: int
    duplicates: int
    errors: int
    results: List[ItemResult]


def _resolve_drum(db, drum_number: Optional[int]) -> tuple[Optional[SilageDrum], Optional[str]]:
    """Devuelve (tambor, error) para el N° indicado.

    Reglas, derivadas de la invariante "un solo tambor abierto":
      - Si el tambor pedido ya está abierto, se usa.
      - Si hay OTRO tambor abierto, es un error explícito: hay que cerrar el
        anterior antes de empezar uno nuevo.
      - Si no hay ninguno abierto, se abre el pedido (creándolo si el N° no
        existe todavía, porque la administración de tambores llega después).
    """
    if drum_number is None:
        return None, "Falta el número de tambor"

    open_drum = db.query(SilageDrum).filter(SilageDrum.status == "open").first()
    if open_drum is not None:
        if open_drum.drum_number == drum_number:
            return open_drum, None
        return None, (
            f"Hay otro tambor abierto (N° {open_drum.drum_number}); "
            f"ciérralo antes de cargar el N° {drum_number}"
        )

    drum = (
        db.query(SilageDrum)
        .filter(SilageDrum.drum_number == drum_number)
        .order_by(SilageDrum.id.desc())
        .first()
    )
    if drum is not None and drum.status in ("sealed", "dispatched"):
        # El N° se reutiliza en terreno tras vaciar el tambor: se abre uno nuevo
        # con el mismo rótulo en vez de reabrir el sellado (que ya salió).
        drum = None

    if drum is None:
        drum = SilageDrum(drum_number=drum_number, status="available",
                          total_kg=0, acid_lts=0, created_at=datetime.now())
        db.add(drum)
        db.flush()

    drum.status = "open"
    drum.opened_at = drum.opened_at or date.today()
    drum.updated_at = datetime.now()
    return drum, None


@router.post("/grinding-events", response_model=BatchResult)
def upload_grinding_events(payload: GrindingBatchIn):
    db = SessionLocal()
    try:
        items = payload.events
        thresholds = load_thresholds(db)
        uuids = [i.client_uuid for i in items]
        existing = {
            r.client_uuid
            for r in db.query(SilageGrindingEvent.client_uuid)
            .filter(SilageGrindingEvent.client_uuid.in_(uuids))
            .all()
        }

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

            log_date = item.log_date or item.event_datetime.date()

            # Resolución del tambor (solo si la carga lleva mortalidad).
            drum = None
            drum_error = None
            if item.had_mortality:
                drum, drum_error = _resolve_drum(db, item.drum_number)
                if drum_error:
                    db.rollback()
                    errors += 1
                    results.append(ItemResult(client_uuid=item.client_uuid, status="error",
                                              error=drum_error))
                    continue

            evaluation = sg.evaluate_event(
                {
                    "log_date": log_date,
                    "had_mortality": item.had_mortality,
                    "mortality_kg": item.mortality_kg,
                    "acid_used": item.acid_used,
                    "acid_lts": item.acid_lts,
                    "additional_acid": item.additional_acid,
                    "ph_value": item.ph_value,
                    "drum_id": drum.id if drum else None,
                    "drum_number": item.drum_number,
                    "drum_closed": item.drum_closed,
                },
                thresholds=thresholds,
                drum_total_kg=drum.total_kg if drum else None,
            )
            if not evaluation.is_valid:
                db.rollback()
                errors += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="error",
                                          error="; ".join(evaluation.errors)))
                continue

            event = SilageGrindingEvent(
                folio=func.nextval("silage_folio_seq"),   # correlativo del servidor
                event_datetime=item.event_datetime,
                log_date=log_date,
                had_mortality=item.had_mortality,
                mortality_kg=item.mortality_kg,
                acid_used=item.acid_used,
                acid_lts=item.acid_lts,
                additional_acid=item.additional_acid,
                ph_value=item.ph_value,
                ph_below_limit=evaluation.ph_below_limit,
                drum_id=drum.id if drum else None,
                drum_number=item.drum_number if drum else None,
                drum_closed=bool(item.drum_closed) if drum else False,
                operator_id=item.operator_id,
                observation=(item.observation or None),
                client_uuid=item.client_uuid,
                created_at=datetime.now(),
            )
            try:
                with db.begin_nested():
                    db.add(event)
                    db.flush()

                    if drum is not None:
                        kg = Decimal(str(item.mortality_kg or 0))
                        lts = Decimal(str(item.acid_lts or 0))
                        drum.total_kg = (drum.total_kg or Decimal(0)) + kg
                        drum.acid_lts = (drum.acid_lts or Decimal(0)) + lts
                        drum.updated_at = datetime.now()
                        if item.drum_closed:
                            # Se sella. NO se abre otro acá: el siguiente se abre
                            # cuando llegue la primera carga que lo referencie.
                            drum.status = "sealed"
                            drum.sealed_at = log_date
                        if lts > 0:
                            db.add(SilageAcidMovement(
                                lot_id=item.acid_lot_id,
                                movement_date=log_date,
                                movement_type="out",
                                lts=lts,
                                grinding_event_id=event.id,
                                drum_id=drum.id,
                                user_id=item.operator_id,
                                notes="Consumo de carga de molienda",
                                created_at=datetime.now(),
                            ))
                db.commit()
                existing.add(item.client_uuid)
                created += 1
                db.refresh(event)
                results.append(ItemResult(
                    client_uuid=item.client_uuid, status="created", id=event.id,
                    folio=event.folio, folio_display=sg.format_folio(event.folio),
                    conformity=evaluation.conformity, drum_status=evaluation.drum_status,
                    warnings=evaluation.warnings,
                ))
            except IntegrityError:
                db.rollback()
                duplicates += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="duplicate"))
            except SQLAlchemyError:
                db.rollback()
                errors += 1
                results.append(ItemResult(client_uuid=item.client_uuid, status="error",
                                          error="no se pudo guardar la carga"))

        return BatchResult(created=created, duplicates=duplicates, errors=errors,
                           results=results)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Cierre del tambor SIN carga asociada
# ---------------------------------------------------------------------------
class CloseDrumIn(BaseModel):
    # El N° que el operador tiene delante. Es lo que hace la operación
    # idempotente: si ese tambor ya está sellado, reintentar no hace nada.
    drum_number: int
    closed_at: Optional[date] = None
    sealed_by: Optional[str] = None
    seal_note: Optional[str] = None


@router.post("/drums/close")
def close_drum(payload: CloseDrumIn):
    """Cierra y sella el tambor abierto, sin registrarle una carga.

    Caso real que no cubría el flujo anterior (donde sellar era una casilla de la
    carga): el tambor va casi lleno, la molienda del día no cabe, y el operador la
    echa en un tambor nuevo dando por terminado el anterior.

    **Idempotente por `drum_number`** y no por `client_uuid`: no hay tabla de
    acciones donde guardar el uuid, pero el estado del tambor ya alcanza. Si el
    tambor pedido está sellado, la respuesta es `already_closed` y no un error —
    importa porque la PWA reintenta desde la cola offline y un reintento no debe
    verse como falla.
    """
    db = SessionLocal()
    try:
        drum = (
            db.query(SilageDrum)
            .filter(SilageDrum.drum_number == payload.drum_number,
                    SilageDrum.status.in_(["open", "sealed"]))
            .order_by(SilageDrum.id.desc())
            .first()
        )
        if drum is None:
            raise HTTPException(
                status_code=404,
                detail=f"No hay tambor N° {payload.drum_number} abierto ni sellado",
            )
        if drum.status == "sealed":
            return {"status": "already_closed", "drum_number": drum.drum_number,
                    "total_kg": float(drum.total_kg or 0),
                    "sealed_at": drum.sealed_at.isoformat() if drum.sealed_at else None}

        drum.status = "sealed"
        drum.sealed_at = payload.closed_at or date.today()
        drum.sealed_by = (payload.sealed_by or None)
        drum.seal_note = (payload.seal_note or None)
        drum.updated_at = datetime.now()
        db.commit()

        # No se abre otro acá: el siguiente se abre solo cuando llegue la primera
        # carga que lo referencie (ver docstring del módulo).
        nxt = (db.query(SilageDrum).filter(SilageDrum.status == "available")
               .order_by(SilageDrum.drum_number).first())
        return {
            "status": "closed",
            "drum_number": drum.drum_number,
            "total_kg": float(drum.total_kg or 0),
            "sealed_at": drum.sealed_at.isoformat(),
            "next_drum_number": nxt.drum_number if nxt else None,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Anulación (única forma de corregir: la captura es aditiva)
# ---------------------------------------------------------------------------
class VoidIn(BaseModel):
    reason: str
    voided_by: Optional[str] = None


@router.post("/grinding-events/{event_id}/void")
def void_grinding_event(event_id: int, payload: VoidIn):
    db = SessionLocal()
    try:
        event = db.get(SilageGrindingEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Carga no encontrada")
        if event.voided_at is not None:
            return {"status": "already_voided", "id": event.id,
                    "voided_at": event.voided_at.isoformat()}
        if not (payload.reason or "").strip():
            raise HTTPException(status_code=400, detail="La anulación requiere un motivo")

        event.voided_at = datetime.now()
        event.voided_by = (payload.voided_by or None)
        event.void_reason = payload.reason.strip()
        event.updated_at = datetime.now()

        # Revertir los efectos de inventario: los kg salen del tambor y el ácido
        # vuelve con un movimiento inverso (no se borra el consumo original).
        drum = db.get(SilageDrum, event.drum_id) if event.drum_id else None
        if drum is not None:
            drum.total_kg = (drum.total_kg or Decimal(0)) - (event.mortality_kg or Decimal(0))
            drum.acid_lts = (drum.acid_lts or Decimal(0)) - (event.acid_lts or Decimal(0))
            drum.updated_at = datetime.now()
        if event.acid_lts and event.acid_lts > 0:
            db.add(SilageAcidMovement(
                movement_date=date.today(),
                movement_type="adjustment",
                lts=event.acid_lts,
                grinding_event_id=event.id,
                drum_id=event.drum_id,
                notes=f"Reverso por anulación de carga {sg.format_folio(event.folio)}",
                created_at=datetime.now(),
            ))
        db.commit()
        return {"status": "voided", "id": event.id,
                "folio_display": sg.format_folio(event.folio)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Recepción por un segundo usuario
# ---------------------------------------------------------------------------
class ReceiveIn(BaseModel):
    # TEXTO ESCRITO: CrianzaApp no tiene autenticación, así que esto no acredita
    # identidad y no debe presentarse como "firma".
    received_by_name: str
    reception_note: Optional[str] = None


@router.post("/grinding-events/{event_id}/receive")
def receive_grinding_event(event_id: int, payload: ReceiveIn):
    db = SessionLocal()
    try:
        event = db.get(SilageGrindingEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Carga no encontrada")
        if event.voided_at is not None:
            raise HTTPException(status_code=400, detail="La carga está anulada")
        if not (payload.received_by_name or "").strip():
            raise HTTPException(status_code=400, detail="Falta el nombre de quien recibe")
        if event.received_at is not None:
            # Idempotente: recibir dos veces no pisa la recepción original.
            return {"status": "already_received", "id": event.id,
                    "received_by_name": event.received_by_name,
                    "received_at": event.received_at.isoformat()}

        event.received_by_name = payload.received_by_name.strip()[:120]
        event.received_at = datetime.now()
        event.reception_note = (payload.reception_note or None)
        event.updated_at = datetime.now()
        db.commit()
        return {"status": "received", "id": event.id,
                "received_by_name": event.received_by_name,
                "received_at": event.received_at.isoformat()}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Inspección semanal (upsert: es única por semana)
# ---------------------------------------------------------------------------
class WeeklyInspectionIn(BaseModel):
    client_uuid: Optional[str] = None
    week_start: date
    cleanliness_ok: bool = False
    drums_sealed_ok: bool = False
    drums_sealed_count: Optional[int] = None
    acid_used_lts: Optional[float] = None
    acid_available_lts: Optional[float] = None
    drums_used: Optional[int] = None
    drums_available: Optional[int] = None
    acid_lot: Optional[str] = None
    responsible_name: Optional[str] = None
    inspector_name: Optional[str] = None
    observation: Optional[str] = None


@router.post("/weekly-inspections")
def upsert_weekly_inspection(payload: WeeklyInspectionIn):
    db = SessionLocal()
    try:
        # El lunes de la semana ISO, sea cual sea el día que mandó el cliente.
        week_start = payload.week_start - timedelta(days=payload.week_start.weekday())
        row = (
            db.query(SilageWeeklyInspection)
            .filter(SilageWeeklyInspection.week_start == week_start)
            .first()
        )
        status = "updated" if row else "created"
        if row is None:
            row = SilageWeeklyInspection(week_start=week_start, created_at=datetime.now())
            db.add(row)

        row.cleanliness_ok = payload.cleanliness_ok
        row.drums_sealed_ok = payload.drums_sealed_ok
        row.drums_sealed_count = payload.drums_sealed_count
        row.acid_used_lts = payload.acid_used_lts
        row.acid_available_lts = payload.acid_available_lts
        row.drums_used = payload.drums_used
        row.drums_available = payload.drums_available
        row.acid_lot = payload.acid_lot
        row.responsible_name = payload.responsible_name
        row.inspector_name = payload.inspector_name
        row.observation = payload.observation
        row.inspected_at = datetime.now()
        row.client_uuid = payload.client_uuid or row.client_uuid
        row.updated_at = datetime.now()
        db.commit()
        db.refresh(row)

        # Contraste informativo con lo que calcula el sistema. Lo declarado por
        # el inspector se conserva tal cual: acá solo se reporta el descuadre.
        events = (
            db.query(SilageGrindingEvent)
            .filter(SilageGrindingEvent.log_date >= week_start,
                    SilageGrindingEvent.log_date < week_start + timedelta(days=7))
            .all()
        )
        totals = sg.build_period_totals(events)
        variances = sg.compare_weekly_declaration(row, totals, acid_available(db))
        return {
            "status": status,
            "id": row.id,
            "week_start": week_start.isoformat(),
            "computed": {
                "total_kg": float(totals.total_kg),
                "total_acid_lts": float(totals.total_acid_lts),
                "events_count": totals.events_count,
                "drums_used": totals.drums_used,
                "drums_sealed": totals.drums_sealed,
            },
            "variances": [
                {
                    "item": v.item,
                    "declared": float(v.declared) if v.declared is not None else None,
                    "computed": float(v.computed) if v.computed is not None else None,
                    "delta": float(v.delta) if v.delta is not None else None,
                    "matches": v.matches,
                }
                for v in variances
            ],
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Consulta: cargas por rango (la usa la PWA para reconstruir el día)
# ---------------------------------------------------------------------------
@router.get("/grinding-events")
def list_grinding_events(desde: Optional[date] = None, hasta: Optional[date] = None,
                         limit: int = 200):
    db = SessionLocal()
    try:
        query = db.query(SilageGrindingEvent)
        if desde:
            query = query.filter(SilageGrindingEvent.log_date >= desde)
        if hasta:
            query = query.filter(SilageGrindingEvent.log_date <= hasta)
        rows = (
            query.order_by(SilageGrindingEvent.event_datetime.desc())
            .limit(max(1, min(limit, 1000)))
            .all()
        )
        thresholds = load_thresholds(db)
        summaries = sg.build_daily_summaries(rows, thresholds)
        return {
            "events": [_serialize_event(e) for e in rows],
            "daily": [
                {
                    "log_date": day.isoformat() if day else None,
                    "total_kg": float(s.total_kg),
                    "events_count": s.events_count,
                    "voided_count": s.voided_count,
                    "drums": s.drums,
                    "ph_min": float(s.ph_min) if s.ph_min is not None else None,
                    "ph_max": float(s.ph_max) if s.ph_max is not None else None,
                    "non_conforming_count": s.non_conforming_count,
                    "pending_reception": s.pending_reception,
                    "no_mortality_declared": s.no_mortality_declared,
                }
                for day, s in summaries.items()
            ],
        }
    finally:
        db.close()
