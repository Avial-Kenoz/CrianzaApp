"""API de sexado offline (checkout / release). [PR-S1]

/api/field/v1/sexado — el cliente PWA toma un estanque fuente + hasta 10
destinos (que quedan bloqueados), trabaja offline, y luego sincroniza (PR-S2).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from urllib.parse import quote_plus

from app.db.session import SessionLocal
from app.models.sexing_offline_operation import SexingOfflineOperation
from app.models.sexado_offline_session import SexadoOfflineSession
from app.models.ponds import Pond
from app.models.users import User
from app.services import sexado_sessions as sx

router = APIRouter(prefix="/api/field/v1/sexado", tags=["sexado"])
admin_router = APIRouter(prefix="/views/ui/sexado", tags=["sexado-admin"])
_jinja = Environment(loader=FileSystemLoader(str(Path(__file__).parent.parent / "templates")))

# kinds que siempre van a reconciliación (no se aplican automático)
CONTINGENCY_KINDS = {"retag", "register", "foreign_tag"}


class CheckoutIn(BaseModel):
    source_pond_id: int
    destination_pond_ids: List[int] = []
    operator_id: Optional[int] = None


class ReleaseIn(BaseModel):
    token: str


@router.get("/ponds")
def picker_ponds():
    """Estanques (padres e hijos) para la selección de fuente/destino."""
    db = SessionLocal()
    try:
        return {"ponds": sx.pond_picker_list(db)}
    finally:
        db.close()


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


# ═══════════════════════════════════════════════════════════════════════════
# Bandeja de reconciliación (vista supervisor) [PR-S2b]
# ═══════════════════════════════════════════════════════════════════════════
@admin_router.get("/reconciliacion", response_class=HTMLResponse)
def reconciliacion(request: Request, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        sessions = (db.query(SexadoOfflineSession)
                    .filter(SexadoOfflineSession.status == "reconciling")
                    .order_by(SexadoOfflineSession.created_at).all())
        pond_names = {p.id: p.name for p in db.query(Pond).all()}
        user_names = {u.id: (" ".join(x for x in [u.name, u.lastname] if x) or u.email)
                      for u in db.query(User).all()}
        blocks = []
        for s in sessions:
            ops = (db.query(SexingOfflineOperation)
                   .filter(SexingOfflineOperation.session_id == s.id,
                           SexingOfflineOperation.status == "pending_review")
                   .order_by(SexingOfflineOperation.id).all())
            rows = []
            for op in ops:
                fish = sx.resolve_fish_by_pit(db, op.pit) if op.pit else None
                rows.append({
                    "id": op.id, "pit": op.pit, "kind": op.kind,
                    "message": op.result_message, "payload": op.payload or {},
                    "captured_at": op.captured_at,
                    "fish_state": (fish.state if fish else None),
                    "fish_found": fish is not None,
                })
            blocks.append({
                "session_id": s.id,
                "source": pond_names.get(s.source_pond_id, s.source_pond_id),
                "operator": user_names.get(s.operator_id, "—"),
                "created_at": s.created_at,
                "pending": rows,
            })
        html = _jinja.get_template("sexado_reconciliacion.html").render(
            {"request": request, "msg": msg, "blocks": blocks})
        return HTMLResponse(content=html)
    finally:
        db.close()


def _resolve_and_redirect(op_id: int, fn):
    db = SessionLocal()
    try:
        op = db.query(SexingOfflineOperation).filter(SexingOfflineOperation.id == op_id).first()
        if not op:
            return RedirectResponse(url="/views/ui/sexado/reconciliacion?msg=Operación+no+encontrada", status_code=303)
        ok, message = fn(db, op)
        session = db.query(SexadoOfflineSession).filter(SexadoOfflineSession.id == op.session_id).first()
        closed = sx.close_session_if_clean(db, session) if session else False
        msg = message + (" · sesión cerrada, estanques liberados" if closed else "")
        return RedirectResponse(url=f"/views/ui/sexado/reconciliacion?msg={quote_plus(msg)}", status_code=303)
    finally:
        db.close()


@admin_router.post("/op/{op_id}/retry")
def op_retry(op_id: int):
    return _resolve_and_redirect(op_id, sx.retry_operation)


@admin_router.post("/op/{op_id}/revive-retry")
def op_revive_retry(op_id: int):
    return _resolve_and_redirect(op_id, sx.revive_and_retry)


@admin_router.post("/op/{op_id}/dismiss")
def op_dismiss(op_id: int):
    return _resolve_and_redirect(op_id, lambda db, op: (sx.dismiss_operation(db, op) or (True, "Operación descartada")))
