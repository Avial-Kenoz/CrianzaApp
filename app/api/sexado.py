"""API de sexado offline (checkout / release). [PR-S1]

/api/field/v1/sexado — el cliente PWA toma un estanque fuente + hasta 10
destinos (que quedan bloqueados), trabaja offline, y luego sincroniza (PR-S2).
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional

from app.db.session import SessionLocal
from app.services import sexado_sessions as sx

router = APIRouter(prefix="/api/field/v1/sexado", tags=["sexado"])


class CheckoutIn(BaseModel):
    source_pond_id: int
    destination_pond_ids: List[int] = []
    operator_id: Optional[int] = None


class ReleaseIn(BaseModel):
    token: str


@router.post("/checkout")
def checkout(payload: CheckoutIn):
    db = SessionLocal()
    try:
        session, err = sx.create_session(
            db, payload.source_pond_id, payload.destination_pond_ids, payload.operator_id)
        if err:
            return JSONResponse(status_code=409, content={"error": err})
        return sx.build_source_snapshot(db, session)
    finally:
        db.close()


@router.post("/release")
def release(payload: ReleaseIn):
    db = SessionLocal()
    try:
        ok = sx.release_session(db, payload.token)
        if not ok:
            return JSONResponse(status_code=404, content={"error": "Sesión no encontrada o ya cerrada."})
        return {"released": True}
    finally:
        db.close()


@router.get("/sessions")
def list_sessions():
    """Sesiones activas (para supervisión y para soltar candados colgados)."""
    db = SessionLocal()
    try:
        return [{
            "id": s.id, "token": s.token, "operator_id": s.operator_id,
            "source_pond_id": s.source_pond_id, "pond_ids": s.pond_ids,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        } for s in sx.active_sessions(db)]
    finally:
        db.close()
