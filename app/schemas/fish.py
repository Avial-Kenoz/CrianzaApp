from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class FishBase(BaseModel):
    internal_id: str
    lot_id: int
    sex: Optional[str] = None
    state: Optional[str] = 'alive'
    registration_time: Optional[datetime] = None
    devious_time: Optional[datetime] = None
    date_of_death: Optional[datetime] = None
    depuration_start_time: Optional[datetime] = None
    mortality_id: Optional[str] = None

class FishCreate(FishBase):
    pass

class FishRead(FishBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
