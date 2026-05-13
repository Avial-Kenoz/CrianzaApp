from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class PondMovementBase(BaseModel):
    source_pond_id: Optional[int] = None
    destiny_pond_id: Optional[int] = None
    fish_id: Optional[int] = None
    lot_id: Optional[int] = None
    fish_quantity: int
    movement_reason: str
    movement_time: datetime
    folio: Optional[int] = None

class PondMovementCreate(PondMovementBase):
    pass

class PondMovementRead(PondMovementBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
