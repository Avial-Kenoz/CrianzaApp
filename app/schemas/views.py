from pydantic import BaseModel
from typing import List, Optional

class LotSummary(BaseModel):
    id: int
    name: str
    internal_id: Optional[str] = None

    class Config:
        from_attributes = True

class PondSummary(BaseModel):
    id: int
    name: str
    internal_id: Optional[str] = None
    code: Optional[str] = None
    depuration: Optional[bool] = False
    state: Optional[str] = None
    volume: Optional[int] = None
    n_fish: int
    biomass: Optional[float] = None
    avg_weight: Optional[float] = None
    active_lots: List[LotSummary]
    pond_type_name: Optional[str] = None

    class Config:
        from_attributes = True

class CultivationUnitWithPonds(BaseModel):
    id: int
    name: str
    ponds: List[PondSummary]

    class Config:
        from_attributes = True
