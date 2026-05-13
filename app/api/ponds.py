from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.db.session import SessionLocal
from app.models.ponds import Pond
from app.models.cultivation_units import CultivationUnit
from app.models.pond_types import PondType
from app.models.lots import Lot
from app.schemas.ponds import PondCreate, PondRead

router = APIRouter(prefix="/ponds", tags=["ponds"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/", response_model=PondRead)
def create_pond(pond: PondCreate, db: Session = Depends(get_db)):
    # Validar que las relaciones existan si se proporcionan
    if pond.cultivation_unit_id:
        unit = db.query(CultivationUnit).filter(CultivationUnit.id == pond.cultivation_unit_id).first()
        if not unit:
            raise HTTPException(status_code=400, detail="Cultivation unit does not exist")
    
    if pond.pond_type_id:
        pond_type = db.query(PondType).filter(PondType.id == pond.pond_type_id).first()
        if not pond_type:
            raise HTTPException(status_code=400, detail="Pond type does not exist")
    
    if pond.lot_id:
        lot = db.query(Lot).filter(Lot.id == pond.lot_id).first()
        if not lot:
            raise HTTPException(status_code=400, detail="Lot does not exist")
    
    db_pond = Pond(**pond.dict())
    db.add(db_pond)
    db.commit()
    db.refresh(db_pond)
    return db_pond

@router.get("/", response_model=List[PondRead])
def list_ponds(db: Session = Depends(get_db)):
    return db.query(Pond).all()

@router.get("/{pond_id}", response_model=PondRead)
def get_pond(pond_id: int, db: Session = Depends(get_db)):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")
    return pond
