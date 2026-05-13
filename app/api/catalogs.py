from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.db.session import SessionLocal
from app.models.species import Species
from app.models.lots import Lot
from app.models.cultivation_units import CultivationUnit
from app.models.pond_types import PondType
from app.schemas.catalogs import (
    SpeciesCreate, SpeciesRead,
    CultivationUnitCreate, CultivationUnitRead,
    PondTypeCreate, PondTypeRead
)
from app.schemas.lots import LotCreate, LotRead

router = APIRouter(prefix="/catalogs", tags=["catalogs"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Species endpoints
@router.get("/species/", response_model=List[SpeciesRead])
def list_species(db: Session = Depends(get_db)):
    return db.query(Species).all()

@router.post("/species/", response_model=SpeciesRead)
def create_species(species: SpeciesCreate, db: Session = Depends(get_db)):
    db_species = Species(**species.dict())
    db.add(db_species)
    db.commit()
    db.refresh(db_species)
    return db_species

@router.get("/species/{species_id}", response_model=SpeciesRead)
def get_species(species_id: int, db: Session = Depends(get_db)):
    species = db.query(Species).filter(Species.id == species_id).first()
    if not species:
        raise HTTPException(status_code=404, detail="Species not found")
    return species

# Lots endpoints
@router.get("/lots/", response_model=List[LotRead])
def list_lots(db: Session = Depends(get_db)):
    return db.query(Lot).all()

@router.post("/lots/", response_model=LotRead)
def create_lot(lot: LotCreate, db: Session = Depends(get_db)):
    # Validar que la especie existe
    species = db.query(Species).filter(Species.id == lot.species_id).first()
    if not species:
        raise HTTPException(status_code=400, detail="Species does not exist")
    
    db_lot = Lot(**lot.dict())
    db.add(db_lot)
    db.commit()
    db.refresh(db_lot)
    return db_lot

@router.get("/lots/{lot_id}", response_model=LotRead)
def get_lot(lot_id: int, db: Session = Depends(get_db)):
    lot = db.query(Lot).filter(Lot.id == lot_id).first()
    if not lot:
        raise HTTPException(status_code=404, detail="Lot not found")
    return lot

# Cultivation Units endpoints
@router.get("/cultivation-units/", response_model=List[CultivationUnitRead])
def list_cultivation_units(db: Session = Depends(get_db)):
    return db.query(CultivationUnit).all()

@router.post("/cultivation-units/", response_model=CultivationUnitRead)
def create_cultivation_unit(unit: CultivationUnitCreate, db: Session = Depends(get_db)):
    db_unit = CultivationUnit(**unit.dict())
    db.add(db_unit)
    db.commit()
    db.refresh(db_unit)
    return db_unit

@router.get("/cultivation-units/{unit_id}", response_model=CultivationUnitRead)
def get_cultivation_unit(unit_id: int, db: Session = Depends(get_db)):
    unit = db.query(CultivationUnit).filter(CultivationUnit.id == unit_id).first()
    if not unit:
        raise HTTPException(status_code=404, detail="Cultivation unit not found")
    return unit

# Pond Types endpoints
@router.get("/pond-types/", response_model=List[PondTypeRead])
def list_pond_types(db: Session = Depends(get_db)):
    return db.query(PondType).all()

@router.post("/pond-types/", response_model=PondTypeRead)
def create_pond_type(pond_type: PondTypeCreate, db: Session = Depends(get_db)):
    db_pond_type = PondType(**pond_type.dict())
    db.add(db_pond_type)
    db.commit()
    db.refresh(db_pond_type)
    return db_pond_type

@router.get("/pond-types/{pond_type_id}", response_model=PondTypeRead)
def get_pond_type(pond_type_id: int, db: Session = Depends(get_db)):
    pond_type = db.query(PondType).filter(PondType.id == pond_type_id).first()
    if not pond_type:
        raise HTTPException(status_code=404, detail="Pond type not found")
    return pond_type
