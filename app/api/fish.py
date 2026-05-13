from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
from app.db.session import SessionLocal
from app.models.fish import Fish
from app.models.ponds_movements import PondMovement
from app.schemas.fish import FishCreate, FishRead

router = APIRouter(prefix="/fish", tags=["fish"])


def _normalize_pit_tag(value: Optional[str]) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()

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
