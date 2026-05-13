from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from app.db.session import SessionLocal
from app.models.depuration_periodic_samplings import DepurationPeriodicSampling
from app.models.fish import Fish
from app.models.ponds import Pond
from app.schemas.depuration_periodic_samplings import DepurationPeriodicSamplingCreate, DepurationPeriodicSamplingRead

router = APIRouter(prefix="/depuration-samplings", tags=["depuration-samplings"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/", response_model=List[DepurationPeriodicSamplingRead])
def list_depuration_samplings(
    db: Session = Depends(get_db),
    fish_id: Optional[int] = None,
    pond_id: Optional[int] = None,
    from_: Optional[str] = None,
    to: Optional[str] = None
):
    query = db.query(DepurationPeriodicSampling)
    if fish_id:
        query = query.filter(DepurationPeriodicSampling.fish_id == fish_id)
    if pond_id:
        query = query.filter(DepurationPeriodicSampling.pond_id == pond_id)
    if from_:
        query = query.filter(DepurationPeriodicSampling.created_at >= from_)
    if to:
        query = query.filter(DepurationPeriodicSampling.created_at <= to)
    return query.order_by(DepurationPeriodicSampling.created_at.desc()).all()

@router.post("/", response_model=DepurationPeriodicSamplingRead)
def create_depuration_sampling(sampling: DepurationPeriodicSamplingCreate, db: Session = Depends(get_db)):
    # Validar que el pez existe si se proporciona
    if sampling.fish_id:
        fish = db.query(Fish).filter(Fish.id == sampling.fish_id).first()
        if not fish:
            raise HTTPException(status_code=400, detail="Fish does not exist")
        # Validar que el pez está en estado de depuración
        if fish.state != "depuration":
            raise HTTPException(status_code=400, detail="Fish must be in depuration state")
    
    # Validar que el estanque existe y es de depuración
    if sampling.pond_id:
        pond = db.query(Pond).filter(Pond.id == sampling.pond_id).first()
        if not pond:
            raise HTTPException(status_code=400, detail="Pond does not exist")
        if not pond.depuration:
            raise HTTPException(status_code=400, detail="Pond must be a depuration pond")
    
    db_sampling = DepurationPeriodicSampling(**sampling.dict())
    db.add(db_sampling)
    db.commit()
    db.refresh(db_sampling)
    return db_sampling
