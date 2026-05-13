from datetime import datetime
import hashlib
import hmac
import json
import os
import time as pytime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
from app.db.session import SessionLocal
from app.models.fish import Fish
from app.models.ponds_movements import PondMovement
from app.models.ponds import Pond
from app.models.cultivation_units import CultivationUnit
from app.api.views import (
    _adjust_biomass_on_movement,
    _recalc_pond_biomass,
    _refresh_pond_runtime_cache_many,
)
from app.schemas.fish import FishCreate, FishRead

router = APIRouter(prefix="/fish", tags=["fish"])

INTEGRATION_SHARED_SECRET = (os.getenv("INTEGRATION_SHARED_SECRET") or "kenoz-integration-dev-secret-change-me").strip()
INTEGRATION_ALLOWED_KEYS = {
    key.strip().lower()
    for key in (os.getenv("INTEGRATION_ALLOWED_KEYS_CRIANZA") or "plantaapp").split(",")
    if key.strip()
}
INTEGRATION_ALLOWED_SKEW_SECONDS = max(30, int(os.getenv("INTEGRATION_ALLOWED_SKEW_SECONDS") or "300"))
INTEGRATION_NONCE_TTL_SECONDS = int(os.getenv("INTEGRATION_NONCE_TTL_SECONDS") or "600")
INTEGRATION_MAX_BODY_BYTES = int(os.getenv("INTEGRATION_MAX_BODY_BYTES") or "2000000")
INTEGRATION_USED_NONCES: dict[str, int] = {}


def _normalize_pit_tag(value: Optional[str]) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()


def _integration_cleanup_used_nonces(now_ts: int):
    expired = [nonce for nonce, exp in INTEGRATION_USED_NONCES.items() if exp < now_ts]
    for nonce in expired:
        INTEGRATION_USED_NONCES.pop(nonce, None)


def _latest_pond_for_fish(fish_id: int, db: Session) -> Optional[int]:
    latest_mv = (
        db.query(PondMovement.destiny_pond_id)
        .filter(PondMovement.fish_id == fish_id)
        .order_by(PondMovement.movement_time.desc(), PondMovement.id.desc())
        .first()
    )
    if not latest_mv or not latest_mv[0]:
        return None
    return int(latest_mv[0])


def _is_jaula_pond(pond_id: Optional[int], db: Session) -> bool:
    if not pond_id:
        return False
    unit_name = (
        db.query(CultivationUnit.name)
        .join(Pond, Pond.cultivation_unit_id == CultivationUnit.id)
        .filter(Pond.id == pond_id)
        .scalar()
    )
    return "jaula" in str(unit_name or "").strip().lower()


def _verify_integration_request_headers(raw_body: bytes, headers) -> str:
    if not INTEGRATION_SHARED_SECRET:
        raise HTTPException(status_code=503, detail="Integración no disponible: secreto no configurado")

    source_key = (headers.get("x-integration-key") or "").strip().lower()
    ts_raw = (headers.get("x-integration-timestamp") or "").strip()
    nonce = (headers.get("x-integration-nonce") or "").strip()
    signature = (headers.get("x-integration-signature") or "").strip().lower()

    if not source_key or source_key not in INTEGRATION_ALLOWED_KEYS:
        raise HTTPException(status_code=401, detail="Integración no autorizada")
    if not ts_raw or not nonce or not signature:
        raise HTTPException(status_code=401, detail="Headers de integración incompletos")
    if len(nonce) < 16:
        raise HTTPException(status_code=401, detail="Nonce inválido")
    if len(raw_body) > INTEGRATION_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload de integración demasiado grande")

    try:
        ts_int = int(ts_raw)
    except ValueError:
        raise HTTPException(status_code=401, detail="Timestamp inválido")

    now_ts = int(pytime.time())
    if abs(now_ts - ts_int) > INTEGRATION_ALLOWED_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="Timestamp fuera de ventana permitida")

    _integration_cleanup_used_nonces(now_ts)
    existing_expiry = INTEGRATION_USED_NONCES.get(nonce)
    if existing_expiry and existing_expiry >= now_ts:
        raise HTTPException(status_code=409, detail="Nonce reutilizado")

    signed_message = f"{ts_raw}.{nonce}.".encode("utf-8") + raw_body
    expected_signature = hmac.new(
        INTEGRATION_SHARED_SECRET.encode("utf-8"),
        signed_message,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=401, detail="Firma inválida")

    INTEGRATION_USED_NONCES[nonce] = now_ts + INTEGRATION_NONCE_TTL_SECONDS
    return source_key

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/", response_model=FishRead)
def create_fish(fish: FishCreate, db: Session = Depends(get_db)):
    normalized_internal_id = _normalize_pit_tag(fish.internal_id)
    if not normalized_internal_id:
        raise HTTPException(status_code=400, detail="internal_id is required")

    # Validar que no exista otro pez con el mismo internal_id y lot_id
    existing = db.query(Fish).filter(Fish.internal_id == normalized_internal_id, Fish.lot_id == fish.lot_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Fish with this internal_id and lot_id already exists")
    # Validar que el lote exista
    from app.models.lots import Lot
    lot = db.query(Lot).filter(Lot.id == fish.lot_id).first()
    if not lot:
        raise HTTPException(status_code=400, detail="Lot does not exist")
    payload = fish.dict()
    payload["internal_id"] = normalized_internal_id
    db_fish = Fish(**payload)
    db.add(db_fish)
    db.commit()
    db.refresh(db_fish)
    return db_fish

@router.get("/", response_model=List[FishRead])
def list_fish(db: Session = Depends(get_db)):
    return db.query(Fish).all()

@router.get("/search/", response_model=List[FishRead])
def search_fish(
    db: Session = Depends(get_db),
    internal_id: Optional[str] = None,
    lot: Optional[int] = None
):
    query = db.query(Fish)
    if internal_id:
        normalized_internal_id = _normalize_pit_tag(internal_id)
        query = query.filter(Fish.internal_id.ilike(f"%{normalized_internal_id}%"))
    if lot:
        query = query.filter(Fish.lot_id == lot)
    return query.all()

@router.get("/{fish_id}", response_model=FishRead)
def get_fish(fish_id: int, db: Session = Depends(get_db)):
    fish = db.query(Fish).filter(Fish.id == fish_id).first()
    if not fish:
        raise HTTPException(status_code=404, detail="Fish not found")
    return fish

@router.get("/{fish_id}/traceability")
def get_fish_traceability(fish_id: int, db: Session = Depends(get_db)):
    fish = db.query(Fish).filter(Fish.id == fish_id).first()
    if not fish:
        raise HTTPException(status_code=404, detail="Fish not found")
    
    movements = db.query(PondMovement).filter(PondMovement.fish_id == fish_id).order_by(PondMovement.movement_time.desc()).all()
    
    return {
        "fish": FishRead.from_orm(fish),
        "movements": [
            {
                "id": m.id,
                "source_pond_id": m.source_pond_id,
                "destiny_pond_id": m.destiny_pond_id,
                "movement_reason": m.movement_reason,
                "movement_time": m.movement_time,
                "fish_quantity": m.fish_quantity
            }
            for m in movements
        ]
    }


class ConfirmFaenaPayload(BaseModel):
    confirmed_at: Optional[datetime] = None  # si APP-FAENA envía su timestamp; si no, se usa utcnow


@router.post("/integration/v1/confirm-faena-crotales")
async def confirm_faena_by_crotales(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    source_key = _verify_integration_request_headers(raw_body, request.headers)

    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON inválido")

    crotales = payload.get("crotales") or []
    if not isinstance(crotales, list):
        raise HTTPException(status_code=400, detail="El campo 'crotales' debe ser una lista")

    confirmed_at_raw = payload.get("confirmed_at")
    confirmed_at = datetime.utcnow()
    if confirmed_at_raw:
        try:
            confirmed_at = datetime.fromisoformat(str(confirmed_at_raw).replace("Z", "+00:00"))
        except ValueError:
            confirmed_at = datetime.utcnow()

    normalized_crotales = sorted({_normalize_pit_tag(item) for item in crotales if _normalize_pit_tag(item)})

    confirmed = []
    already_dead = []
    not_found = []
    skipped_state = []
    auto_sent_from_jaula = []
    affected_pond_ids: set[int] = set()
    generated_movements: list[PondMovement] = []

    for crotal in normalized_crotales:
        fish = db.query(Fish).filter(func.upper(Fish.internal_id) == crotal).order_by(Fish.id.desc()).first()
        if not fish:
            not_found.append(crotal)
            continue

        source_pond_id = _latest_pond_for_fish(fish.id, db)
        if source_pond_id:
            affected_pond_ids.add(int(source_pond_id))

        if fish.state == "faena":
            fish.state = "dead"
            fish.date_of_death = confirmed_at
            fish.updated_at = datetime.utcnow()
            confirmed.append(crotal)
            continue

        if fish.state == "dead":
            already_dead.append(crotal)
            continue

        if fish.state in ("alive", "depuration"):
            if not _is_jaula_pond(source_pond_id, db):
                skipped_state.append({"crotal": crotal, "state": fish.state, "reason": "not_in_jaula"})
                continue

            now = datetime.utcnow()
            movement = PondMovement(
                source_pond_id=source_pond_id,
                destiny_pond_id=None,
                fish_id=fish.id,
                lot_id=fish.lot_id,
                fish_quantity=1,
                movement_reason="faena",
                movement_time=confirmed_at,
                created_at=now,
                updated_at=now,
            )
            db.add(movement)
            generated_movements.append(movement)

            fish.state = "dead"
            fish.date_of_death = confirmed_at
            fish.updated_at = now
            confirmed.append(crotal)
            auto_sent_from_jaula.append(crotal)
            continue

        skipped_state.append({"crotal": crotal, "state": fish.state})

    db.flush()
    for movement in generated_movements:
        _adjust_biomass_on_movement(movement, db)
    for pond_id in sorted(affected_pond_ids):
        _recalc_pond_biomass(pond_id, db)
    _refresh_pond_runtime_cache_many(affected_pond_ids, db)

    db.commit()

    return {
        "ok": True,
        "source_key": source_key,
        "requested": len(normalized_crotales),
        "confirmed": confirmed,
        "already_dead": already_dead,
        "not_found": not_found,
        "skipped_state": skipped_state,
        "auto_sent_from_jaula": auto_sent_from_jaula,
    }


@router.post("/{fish_id}/confirm-faena")
def confirm_faena(fish_id: int, payload: ConfirmFaenaPayload = ConfirmFaenaPayload(), db: Session = Depends(get_db)):
    """
    Confirma la recepción del pez en planta de faena.
    Puede ser invocado desde la UI interna o desde APP-FAENA vía API.
    Cambia fish.state 'faena' → 'dead' y registra fish.date_of_death.
    """
    fish = db.query(Fish).filter(Fish.id == fish_id).first()
    if not fish:
        raise HTTPException(status_code=404, detail="Fish not found")
    if fish.state != "faena":
        raise HTTPException(
            status_code=409,
            detail=f"Fish is in state '{fish.state}', expected 'faena'"
        )

    confirmed_at = payload.confirmed_at or datetime.utcnow()
    fish.state = "dead"
    fish.date_of_death = confirmed_at
    fish.updated_at = datetime.utcnow()
    db.commit()

    return {
        "fish_id": fish.id,
        "internal_id": fish.internal_id,
        "state": fish.state,
        "date_of_death": fish.date_of_death,
    }
