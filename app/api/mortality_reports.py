from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from app.db.session import SessionLocal
from app.models.mortality_reports import MortalityReport
from app.models.marked_mortality_registries import MarkedMortalityRegistry
from app.models.unmarked_mortality_registries import UnmarkedMortalityRegistry
from app.schemas.mortality_reports import (
    MortalityReportCreate, MortalityReportRead,
    MarkedMortalityRegistryCreate, MarkedMortalityRegistryRead,
    UnmarkedMortalityRegistryCreate, UnmarkedMortalityRegistryRead
)

router = APIRouter(prefix="/mortality-reports", tags=["mortality-reports"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/", response_model=List[MortalityReportRead])
def list_mortality_reports(
    db: Session = Depends(get_db),
    from_: Optional[str] = None,
    to: Optional[str] = None,
    validated: Optional[bool] = None
):
    query = db.query(MortalityReport)
    if from_:
        query = query.filter(MortalityReport.registry_time >= from_)
    if to:
        query = query.filter(MortalityReport.registry_time <= to)
    if validated is not None:
        if validated:
            query = query.filter((MortalityReport.supervisor_validation == True) | (MortalityReport.general_validation == True))
        else:
            query = query.filter((MortalityReport.supervisor_validation == False) & (MortalityReport.general_validation == False))
    return query.order_by(MortalityReport.registry_time.desc()).all()

@router.post("/", response_model=MortalityReportRead)
def create_mortality_report(report: MortalityReportCreate, db: Session = Depends(get_db)):
    # Establecer timestamp de registro si no se proporciona
    if not report.registry_time:
        report.registry_time = datetime.utcnow()
    
    db_report = MortalityReport(**report.dict())
    db.add(db_report)
    db.commit()
    db.refresh(db_report)
    return db_report

@router.post("/{report_id}/marked", response_model=MarkedMortalityRegistryRead)
def create_marked_mortality(report_id: int, registry: MarkedMortalityRegistryCreate, db: Session = Depends(get_db)):
    # Validar que el reporte exista
    report = db.query(MortalityReport).filter(MortalityReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Mortality report not found")
    
    # Establecer timestamp de registro si no se proporciona
    if not registry.registry_time:
        registry.registry_time = datetime.utcnow()
    
    # Asegurar que se asigne al reporte correcto
    registry.mortality_report_id = report_id
    
    db_registry = MarkedMortalityRegistry(**registry.dict())
    db.add(db_registry)
    db.commit()
    db.refresh(db_registry)
    return db_registry

@router.post("/{report_id}/unmarked", response_model=UnmarkedMortalityRegistryRead)
def create_unmarked_mortality(report_id: int, registry: UnmarkedMortalityRegistryCreate, db: Session = Depends(get_db)):
    # Validar que el reporte exista
    report = db.query(MortalityReport).filter(MortalityReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Mortality report not found")
    
    # Establecer timestamp de registro si no se proporciona
    if not registry.registry_time:
        registry.registry_time = datetime.utcnow()
    
    # Asegurar que se asigne al reporte correcto
    registry.mortality_report_id = report_id
    
    db_registry = UnmarkedMortalityRegistry(**registry.dict())
    db.add(db_registry)
    db.commit()
    db.refresh(db_registry)
    return db_registry
