from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class FishSamplingBase(BaseModel):
    user_id: Optional[int] = None
    fish_id: int
    length: Optional[float] = None
    weight: Optional[float] = None
    oocyte_size: Optional[float] = None
    color: Optional[str] = None
    gonad_size: Optional[float] = None
    flavor: Optional[str] = None
    development_state: Optional[str] = None
    harvest_date: Optional[datetime] = None
    registry_time: Optional[datetime] = None
    diameter: Optional[float] = None
    agglomeration: Optional[int] = None
    turgor: Optional[int] = None
    fat: Optional[int] = None

class FishSamplingCreate(FishSamplingBase):
    pass

class FishSamplingRead(FishSamplingBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
