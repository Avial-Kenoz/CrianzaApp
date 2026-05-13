from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class MortalityReportBase(BaseModel):
    comment: Optional[str] = None
    registry_time: Optional[datetime] = None
    breeding_user_id: Optional[int] = None
    supervisor_user_id: Optional[int] = None
    general_user_id: Optional[int] = None
    supervisor_validation: Optional[bool] = False
    general_validation: Optional[bool] = False
    supervisor_validation_time: Optional[datetime] = None
    general_validation_time: Optional[datetime] = None

class MortalityReportCreate(MortalityReportBase):
    pass

class MortalityReportRead(MortalityReportBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class MarkedMortalityRegistryBase(BaseModel):
    mortality_report_id: int
    ponds_movement_id: Optional[int] = None
    comment: Optional[str] = None
    registry_time: Optional[datetime] = None
    supervisor_validation: Optional[bool] = False
    general_validation: Optional[bool] = False

class MarkedMortalityRegistryCreate(MarkedMortalityRegistryBase):
    pass

class MarkedMortalityRegistryRead(MarkedMortalityRegistryBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class UnmarkedMortalityRegistryBase(BaseModel):
    mortality_report_id: int
    ponds_movement_id: Optional[int] = None
    comment: Optional[str] = None
    registry_time: Optional[datetime] = None
    supervisor_validation: Optional[bool] = False
    general_validation: Optional[bool] = False

class UnmarkedMortalityRegistryCreate(UnmarkedMortalityRegistryBase):
    pass

class UnmarkedMortalityRegistryRead(UnmarkedMortalityRegistryBase):
    id: int
    legacy_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
