from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class PondBase(BaseModel):
    name: str
    internal_id: Optional[str] = None
    code: Optional[str] = None
    cultivation_unit_id: Optional[int] = None
    pond_type_id: Optional[int] = None
    lot_id: Optional[int] = None
    volume: Optional[int] = 0
    depuration: Optional[bool] = False
    state: Optional[str] = 'success'
    biomass: Optional[float] = None
    avg_weight: Optional[float] = None

class PondCreate(PondBase):
    pass

class PondRead(PondBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
