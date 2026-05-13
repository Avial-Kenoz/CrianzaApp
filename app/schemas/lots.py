from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class LotBase(BaseModel):
    species_id: int
    name: str
    internal_id: Optional[str] = None
    origin: Optional[str] = None
    hatch_year: Optional[int] = None
    creation_time: Optional[datetime] = None
    creation_date: Optional[datetime] = None
    national: Optional[bool] = False

class LotCreate(LotBase):
    pass

class LotRead(LotBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
