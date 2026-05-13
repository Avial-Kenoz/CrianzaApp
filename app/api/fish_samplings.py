from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from app.db.session import SessionLocal
from app.models.fish_samplings import FishSampling
from app.models.fish import Fish
from app.schemas.fish_samplings import FishSamplingCreate, FishSamplingRead

router = APIRouter(prefix="/fish-samplings", tags=["fish-samplings"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/", response_model=List[FishSamplingRead])
def list_fish_samplings(
    db: Session = Depends(get_db),
    fish_id: Optional[int] = None,
    from_: Optional[str] = None,
    to: Optional[str] = None
):
    query = db.query(FishSampling)
    if fish_id:
        query = query.filter(FishSampling.fish_id == fish_id)
    if from_:
        query = query.filter(FishSampling.registry_time >= from_)
    if to:
        query = query.filter(FishSampling.registry_time <= to)
    return query.order_by(FishSampling.registry_time.desc()).all()

@router.post("/", response_model=FishSamplingRead)
def create_fish_sampling(sampling: FishSamplingCreate, db: Session = Depends(get_db)):
    fish = db.query(Fish).filter(Fish.id == sampling.fish_id).first()
    if not fish:
        raise HTTPException(status_code=400, detail="Fish does not exist")

    if not sampling.registry_time:
        sampling.registry_time = datetime.utcnow()

    db_sampling = FishSampling(**sampling.dict())
    db.add(db_sampling)
    db.commit()
    db.refresh(db_sampling)
    return db_sampling
