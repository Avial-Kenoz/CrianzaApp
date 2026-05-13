from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class SpeciesBase(BaseModel):
    name: str
    internal_id: Optional[str] = None

class SpeciesCreate(SpeciesBase):
    pass

class SpeciesRead(SpeciesBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class CultivationUnitBase(BaseModel):
    name: str

class CultivationUnitCreate(CultivationUnitBase):
    pass

class CultivationUnitRead(CultivationUnitBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class PondTypeBase(BaseModel):
    name: str

class PondTypeCreate(PondTypeBase):
    pass

class PondTypeRead(PondTypeBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
