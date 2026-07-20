"""API de sexado offline (checkout / release). [PR-S1]

/api/field/v1/sexado — el cliente PWA toma un estanque fuente + hasta 10
destinos (que quedan bloqueados), trabaja offline, y luego sincroniza (PR-S2).
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

from app.db.session import SessionLocal
from app.models.sexing_offline_operation import SexingOfflineOperation
from app.services import sexado_sessions as sx

router = APIRouter(prefix="/api/field/v1/sexado", tags=["sexado"])

# kinds que siempre van a reconciliación (no se aplican automático)
CONTINGENCY_KINDS = {"retag", "register", "foreign_tag"}


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


class SexadoOpIn(BaseModel):
    client_uuid: str
    kind: str = "fish_save"        # fish_save | retag | register | foreign_tag
    pit: Optional[str] = None
    fish_id: Optional[int] = None
    sex: Optional[str] = None
    weight: Optional[float] = None
    diameter: Optional[float] = None
    development_state: Optional[str] = None
    move_to: Optional[int] = None  # id de estanque destino
    captured_at: Optional[datetime] = None
    extra: Optional[dict] = None


class SyncIn(BaseModel):
    token: str
    operations: List[SexadoOpIn] = []


@router.post("/sync")
def sync(payload: SyncIn):
    from app.api.views import apply_fish_save  # import perezoso (evita ciclo)
    db = SessionLocal()
    try:
        session = sx.session_by_token(db, payload.token)
        if not session:
            return JSONResponse(status_code=404, content={"error": "Sesión no encontrada o ya cerrada."})

        source_id = session.source_pond_id
        dest_ids = {int(p) for p in session.pond_ids if int(p) != source_id}
        existing = {r.client_uuid for r in db.query(SexingOfflineOperation.client_uuid)
                    .filter(SexingOfflineOperation.client_uuid.in_([o.client_uuid for o in payload.operations])).all()}

        results = []
        counts = {"applied": 0, "duplicate": 0, "pending_review": 0, "error": 0}

        def record(op, status, message, fish_id=None):
            counts[status] = counts.get(status, 0) + 1
            results.append({"client_uuid": op.client_uuid, "status": status, "message": message})
            if status == "duplicate":
                return
            db.add(SexingOfflineOperation(
                client_uuid=op.client_uuid, session_id=session.id, pond_id=source_id,
                kind=op.kind, pit=op.pit, fish_id=fish_id,
                payload={"sex": op.sex, "weight": op.weight, "diameter": op.diameter,
                         "development_state": op.development_state, "move_to": op.move_to},
                captured_at=op.captured_at, status=status, result_message=(message or "")[:255],
                applied_at=(datetime.now() if status == "applied" else None),
                created_at=datetime.now(),
            ))
            db.commit()

        for op in payload.operations:
            if op.client_uuid in existing:
                record(op, "duplicate", "Ya sincronizada."); continue

            if op.kind in CONTINGENCY_KINDS:
                record(op, "pending_review", f"Contingencia '{op.kind}' para reconciliar.",
                       fish_id=op.fish_id); continue

            # fish_save: resolver por PIT
            fish = sx.resolve_fish_by_pit(db, op.pit) if op.pit else None
            if fish is None:
                record(op, "pending_review", "PIT no encontrado (retag/tag ajeno).", fish_id=op.fish_id)
                continue
            if op.move_to is not None and op.move_to not in dest_ids:
                record(op, "pending_review", "Destino no está entre los preconfigurados.", fish_id=fish.id)
                continue

            res = apply_fish_save(
                db, source_id, fish.id,
                sex=op.sex,
                weight=(str(op.weight) if op.weight is not None else None),
                diameter=(str(op.diameter) if op.diameter is not None else None),
                development_state=op.development_state,
                move_to=(str(op.move_to) if op.move_to is not None else None),
            )
            if res.ok:
                record(op, "applied", res.message, fish_id=fish.id)
            else:
                # falla de validación (p. ej. pez ya no está en el estanque) -> reconciliar
                record(op, "pending_review", res.message, fish_id=fish.id)

        has_pending = counts["pending_review"] > 0
        sx.finalize_session(db, session, has_pending)

        return {"session_status": session.status, "counts": counts, "results": results}
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
