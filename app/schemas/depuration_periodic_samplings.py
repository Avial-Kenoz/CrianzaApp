from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime

class DepurationPeriodicSamplingBase(BaseModel):
    fish_id: Optional[int] = None
    pond_id: Optional[int] = None
    development_state: Optional[str] = None
    width: Optional[float] = None
    heigh: Optional[float] = None
    weight: Optional[float] = None
    harvest_date: Optional[date] = None
    color: Optional[str] = None
    gonad_dimension: Optional[float] = None
    oocyte_size: Optional[float] = None
    flavor: Optional[str] = None

class DepurationPeriodicSamplingCreate(DepurationPeriodicSamplingBase):
    pass

class DepurationPeriodicSamplingRead(DepurationPeriodicSamplingBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
