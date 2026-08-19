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
from datetime import datetime, timedelta
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

# kinds que siempre van a reconciliación (no se aplican automático).
# 'register' (alta de PIT desde saldo sin marca) SÍ se auto-aplica.
CONTINGENCY_KINDS = {"retag", "foreign_tag"}


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
    kind: str = "fish_save"        # fish_save | move_untagged | retag | register | foreign_tag
    pit: Optional[str] = None
    fish_id: Optional[int] = None
    sex: Optional[str] = None
    weight: Optional[float] = None
    diameter: Optional[float] = None
    development_state: Optional[str] = None
    move_to: Optional[int] = None  # id de estanque destino
    lot_id: Optional[int] = None   # para move_untagged
    quantity: Optional[int] = None  # para move_untagged
    captured_at: Optional[datetime] = None
    extra: Optional[dict] = None


class SyncIn(BaseModel):
    token: str
    operations: List[SexadoOpIn] = []


@router.post("/sync")
def sync(payload: SyncIn):
    from app.api.views import apply_fish_save, apply_register_tagged  # import perezoso (evita ciclo)
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

        def record(op, status, message, fish_id=None, reason=None):
            counts[status] = counts.get(status, 0) + 1
            results.append({"client_uuid": op.client_uuid, "status": status, "message": message})
            if status == "duplicate":
                return
            # el motivo se guarda como código: el tablet lo usa para avisarle al
            # operador qué pierde si descarta (ver conflict_severity)
            if reason is None and status == "pending_review":
                reason = sx.reason_from_message(message)
            db.add(SexingOfflineOperation(
                client_uuid=op.client_uuid, session_id=session.id, pond_id=source_id,
                kind=op.kind, pit=op.pit, fish_id=fish_id,
                payload={"sex": op.sex, "weight": op.weight, "diameter": op.diameter,
                         "development_state": op.development_state, "move_to": op.move_to,
                         "lot_id": op.lot_id, "quantity": op.quantity},
                captured_at=op.captured_at, status=status, result_message=(message or "")[:255],
                reason_code=reason,
                applied_at=(datetime.now() if status == "applied" else None),
                created_at=datetime.now(),
            ))
            db.commit()

        for op in payload.operations:
            if op.client_uuid in existing:
                record(op, "duplicate", "Ya sincronizada."); continue

            if op.kind in CONTINGENCY_KINDS:
                record(op, "pending_review", f"Contingencia '{op.kind}' para reconciliar.",
                       fish_id=op.fish_id, reason=sx.REASON_CONTINGENCY); continue

            if op.kind == "move_untagged":
                if op.move_to is None or op.move_to not in dest_ids:
                    record(op, "pending_review", "Destino no está entre los preconfigurados."); continue
                ok2, msg2 = sx.apply_untagged_move(db, source_id, op.lot_id, op.quantity, op.move_to)
                record(op, "applied" if ok2 else "pending_review", msg2); continue

            if op.kind == "register":
                if op.move_to is not None and op.move_to not in dest_ids:
                    record(op, "pending_review", "Destino no está entre los preconfigurados."); continue
                res = apply_register_tagged(db, source_id, op.pit, lot_id=op.lot_id, sex=op.sex,
                                            weight=op.weight, diameter=op.diameter,
                                            development_state=op.development_state, move_to=op.move_to)
                record(op, "applied" if res.ok else "pending_review", res.message); continue

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

        sx.finalize_session(db, session, counts["pending_review"] > 0)

        out = {"session_status": session.status, "counts": counts, "results": results}
        if session.status == "reconciling":
            # el tablet abre la vista de conflictos con esto, sin otra vuelta a la red
            out["conflicts"] = _conflicts_payload(db, session)["conflicts"]
        return out
    finally:
        db.close()


class ConflictActionIn(BaseModel):
    token: str
    action: str          # retry | dismiss | supervisor


def _conflicts_payload(db, session) -> dict:
    ops = (db.query(SexingOfflineOperation)
           .filter(SexingOfflineOperation.session_id == session.id,
                   SexingOfflineOperation.status == "pending_review")
           .order_by(SexingOfflineOperation.id).all())
    return {"session_status": session.status,
            "conflicts": [sx.conflict_row(db, op) for op in ops]}


@router.get("/conflicts")
def list_conflicts(token: str):
    """Conflictos que el tablet tiene que resolver antes de cerrar la sesión."""
    db = SessionLocal()
    try:
        session = sx.session_by_token_any(db, token)
        if not session:
            return JSONResponse(status_code=404, content={"error": "Sesión no encontrada."})
        return _conflicts_payload(db, session)
    finally:
        db.close()


@router.post("/conflicts/{op_id}/resolve")
def resolve_conflict(op_id: int, payload: ConflictActionIn):
    """Resuelve un conflicto desde el tablet: reintentar, descartar o dejarlo
    para el supervisor. Cuando no queda ninguno, la sesión se cierra sola y el
    estanque se libera."""
    db = SessionLocal()
    try:
        session = sx.session_by_token_any(db, payload.token)
        if not session:
            return JSONResponse(status_code=404, content={"error": "Sesión no encontrada."})
        op = db.query(SexingOfflineOperation).filter(
            SexingOfflineOperation.id == op_id).first()
        # la op tiene que ser de ESTA sesión: el token no da acceso a la bandeja ajena
        if not op or op.session_id != session.id:
            return JSONResponse(status_code=404,
                                content={"error": "Operación no encontrada en esta sesión."})

        ok, message = sx.resolve_conflict(db, op, payload.action)
        # 'supervisor' deja la pendiente viva a propósito: no cierra la sesión
        if ok and payload.action != "supervisor":
            sx.close_session_if_clean(db, session)
        db.refresh(session)
        out = _conflicts_payload(db, session)
        out.update({"ok": ok, "message": message, "op_id": op_id})
        return out
    finally:
        db.close()


@router.get("/sessions")
def list_sessions():
    """Sesiones que siguen bloqueando estanques (activas o en reconciliación).

    La usa el tablet en la pantalla de setup para ofrecer retomar una sesión
    abandonada: si un equipo suelta su token (o se cambia de equipo), sin esto
    los conflictos de esa sesión solo se pueden tocar desde el escritorio.
    """
    db = SessionLocal()
    try:
        out = []
        for s in sx.active_sessions(db):
            pond = db.query(Pond).filter(Pond.id == s.source_pond_id).first()
            out.append({
                "id": s.id, "token": s.token, "operator_id": s.operator_id,
                "source_pond_id": s.source_pond_id, "pond_ids": s.pond_ids,
                "source_name": (pond.name if pond else str(s.source_pond_id)),
                "status": s.status,
                "pending_count": sx.pending_count(db, s.id),
                "created_at": s.created_at.isoformat() if s.created_at else None,
            })
        return out
    finally:
        db.close()


class ResumeIn(BaseModel):
    token: str


@router.post("/resume")
def resume(payload: ResumeIn):
    """Retoma una sesión que sigue bloqueando: devuelve el mismo snapshot que el
    checkout, más los conflictos que hayan quedado esperando."""
    db = SessionLocal()
    try:
        session = sx.session_by_token(db, payload.token)
        if not session:
            return JSONResponse(status_code=404,
                                content={"error": "Sesión no encontrada o ya cerrada."})
        snap = sx.build_source_snapshot(db, session)
        snap["conflicts"] = _conflicts_payload(db, session)["conflicts"]
        snap["session_status"] = session.status
        return snap
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
# Bandeja de reconciliación (vista supervisor) [PR-S2b]
# ═══════════════════════════════════════════════════════════════════════════
@admin_router.get("/reconciliacion", response_class=HTMLResponse)
def reconciliacion(request: Request, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        pond_names = {p.id: p.name for p in db.query(Pond).all()}
        user_names = {u.id: (" ".join(x for x in [u.name, u.lastname] if x) or u.email)
                      for u in db.query(User).all()}

        # Sesiones activas (bloquean estanques, aún sin contingencias): un tablet
        # trabaja offline contra ellas. El supervisor puede liberarlas si el tablet
        # tiene problemas (con confirmación, porque el tablet pierde lo no subido).
        active = (db.query(SexadoOfflineSession)
                  .filter(SexadoOfflineSession.status == "active")
                  .order_by(SexadoOfflineSession.created_at).all())
        active_rows = []
        for s in active:
            nops = (db.query(SexingOfflineOperation)
                    .filter(SexingOfflineOperation.session_id == s.id).count())
            active_rows.append({
                "id": s.id,
                "source": pond_names.get(s.source_pond_id, s.source_pond_id),
                "operator": user_names.get(s.operator_id, "—"),
                "ponds": ", ".join(pond_names.get(int(p), str(p)) for p in (s.pond_ids or [])),
                "created_at": s.created_at,
                "ops": nops,
            })

        # Cerradas en las últimas 24 h: reactivables si un tablet quedó con la cola
        # huérfana (evita tener que tocar la BD a mano).
        since = datetime.now() - timedelta(hours=24)
        closed = (db.query(SexadoOfflineSession)
                  .filter(SexadoOfflineSession.status.in_(["released", "synced"]),
                          SexadoOfflineSession.released_at.isnot(None),
                          SexadoOfflineSession.released_at >= since)
                  .order_by(SexadoOfflineSession.released_at.desc()).all())
        closed_rows = [{
            "id": s.id,
            "source": pond_names.get(s.source_pond_id, s.source_pond_id),
            "operator": user_names.get(s.operator_id, "—"),
            "status": s.status,
            "released_at": s.released_at,
        } for s in closed]

        # Toda sesión con pendientes, no solo las 'reconciling': si una se cierra
        # con operaciones vivas (pasó el 19/08/2026), sus conflictos quedaban
        # invisibles acá y solo se podían tocar por endpoint directo.
        pending_ids = [r[0] for r in
                       db.query(SexingOfflineOperation.session_id)
                       .filter(SexingOfflineOperation.status == "pending_review")
                       .distinct().all() if r[0] is not None]
        sessions = ((db.query(SexadoOfflineSession)
                     .filter(SexadoOfflineSession.id.in_(pending_ids))
                     .order_by(SexadoOfflineSession.created_at).all())
                    if pending_ids else [])
        blocks = []
        for s in sessions:
            ops = (db.query(SexingOfflineOperation)
                   .filter(SexingOfflineOperation.session_id == s.id,
                           SexingOfflineOperation.status == "pending_review")
                   .order_by(SexingOfflineOperation.id).all())
            blocks.append({
                "session_id": s.id,
                "source": pond_names.get(s.source_pond_id, s.source_pond_id),
                "operator": user_names.get(s.operator_id, "—"),
                "created_at": s.created_at,
                "status": s.status,
                "pending": [sx.conflict_row(db, op) for op in ops],
            })
        html = _jinja.get_template("sexado_reconciliacion.html").render(
            {"request": request, "msg": msg, "blocks": blocks,
             "active_rows": active_rows, "closed_rows": closed_rows})
        return HTMLResponse(content=html)
    finally:
        db.close()


@admin_router.post("/session/{session_id}/release")
def session_release(session_id: int):
    """Cierre desde la app (supervisor): libera el candado de una sesión activa."""
    db = SessionLocal()
    try:
        ok, message = sx.release_session_by_id(db, session_id, note="Liberada por supervisor desde la app.")
        return RedirectResponse(url=f"/views/ui/sexado/reconciliacion?msg={quote_plus(message)}", status_code=303)
    finally:
        db.close()


@admin_router.post("/session/{session_id}/reactivate")
def session_reactivate(session_id: int):
    """Reactiva una sesión cerrada para que un tablet huérfano vuelva a sincronizar."""
    db = SessionLocal()
    try:
        ok, message = sx.reactivate_session(db, session_id)
        return RedirectResponse(url=f"/views/ui/sexado/reconciliacion?msg={quote_plus(message)}", status_code=303)
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
