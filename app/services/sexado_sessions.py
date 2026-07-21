"""Sesiones de sexado offline: bloqueo de estanques, snapshot y ciclo de vida.

Sin HTTP. Lo usan el router `/api/field/v1/sexado` y los guards de bloqueo que
los flujos online (sexado, movimientos, mortalidad sin marca) invocan para
rechazar ediciones sobre estanques en sesión.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ponds import Pond
from app.models.fish import Fish
from app.models.fish_samplings import FishSampling
from app.models.ponds_movements import PondMovement
from app.models.lots import Lot
from app.models.sexado_offline_session import SexadoOfflineSession
from app.models.sexing_offline_operation import SexingOfflineOperation

MAX_DESTINATIONS = 10

# Estados de desarrollo válidos por sexo (espejo de la validación online)
VALID_DEV_STATES = {"f": ["0", "1", "2", "3", "4", "R"], "m": ["0", "L"]}

# Estados de sesión que mantienen los estanques bloqueados
LOCKING_STATUSES = ("active", "reconciling")


# ---------------------------------------------------------------------------
# Bloqueo
# ---------------------------------------------------------------------------
def active_sessions(db: Session):
    """Sesiones que aún bloquean estanques (activas o en reconciliación)."""
    return db.query(SexadoOfflineSession).filter(
        SexadoOfflineSession.status.in_(LOCKING_STATUSES)).all()


def pond_picker_list(db: Session) -> list[dict]:
    """Estanques activos (padres e hijos) para elegir fuente/destino, ordenados
    por unidad de cultivo → estanque (padre) → hijo. Incluye si está bloqueado."""
    from app.models.cultivation_units import CultivationUnit
    locked = locked_pond_ids(db)
    units = {u.id: u.name for u in db.query(CultivationUnit).all()}
    ponds = db.query(Pond).filter(Pond.state != "inactive").all()
    by_id = {p.id: p for p in ponds}

    def top_name(p):
        parent = by_id.get(p.parent_pond_id) if p.parent_pond_id else None
        return (parent.name if parent else p.name) or ""

    rows = []
    for p in ponds:
        parent = by_id.get(p.parent_pond_id) if p.parent_pond_id else None
        rows.append({
            "id": p.id, "name": p.name,
            "is_child": parent is not None,
            "parent_name": (parent.name if parent else None),
            "unit_id": p.cultivation_unit_id,
            "unit_name": units.get(p.cultivation_unit_id, "Sin unidad"),
            "locked": p.id in locked,
            "_k": (units.get(p.cultivation_unit_id, "") or "", top_name(p),
                   1 if parent else 0, p.name or ""),
        })
    rows.sort(key=lambda r: r["_k"])
    for r in rows:
        r.pop("_k")
    return rows


def session_by_token(db: Session, token: str) -> Optional[SexadoOfflineSession]:
    return db.query(SexadoOfflineSession).filter(
        SexadoOfflineSession.token == token,
        SexadoOfflineSession.status.in_(LOCKING_STATUSES)).first()


def resolve_fish_by_pit(db: Session, pit: str):
    if not pit:
        return None
    # Canonicaliza el PIT quitando ceros a la izquierda (el lector los antepone:
    # 0007CF009C ≡ 7CF009C) en ambos lados: la entrada y la columna guardada,
    # así matchea tags padded ya existentes sin migrar datos.
    s = str(pit).strip().upper()
    canon = s.lstrip("0") or s
    norm_col = func.upper(func.ltrim(func.trim(Fish.internal_id), "0"))
    return db.query(Fish).filter(norm_col == canon).first()


def finalize_session(db: Session, session: SexadoOfflineSession, has_pending: bool) -> None:
    """Tras sincronizar: si quedan contingencias, la sesión pasa a 'reconciling'
    (sigue bloqueando); si todo quedó limpio, 'synced' y libera el bloqueo."""
    if has_pending:
        session.status = "reconciling"
    else:
        session.status = "synced"
        session.released_at = datetime.now()
    db.commit()


# ---------------------------------------------------------------------------
# Reconciliación (bandeja del supervisor)
# ---------------------------------------------------------------------------
def pending_count(db: Session, session_id: int) -> int:
    return (db.query(SexingOfflineOperation)
            .filter(SexingOfflineOperation.session_id == session_id,
                    SexingOfflineOperation.status == "pending_review").count())


def close_session_if_clean(db: Session, session: SexadoOfflineSession) -> bool:
    """Cierra la sesión (libera el bloqueo) si ya no quedan pendientes."""
    if pending_count(db, session.id) == 0 and session.status in LOCKING_STATUSES:
        session.status = "synced"
        session.released_at = datetime.now()
        db.commit()
        return True
    return False


def _apply_op_payload(db: Session, op: SexingOfflineOperation, fish_id: int):
    from app.api.views import apply_fish_save  # perezoso (evita ciclo)
    p = op.payload or {}
    return apply_fish_save(
        db, op.pond_id, fish_id,
        sex=p.get("sex"),
        weight=(str(p["weight"]) if p.get("weight") is not None else None),
        diameter=(str(p["diameter"]) if p.get("diameter") is not None else None),
        development_state=p.get("development_state"),
        move_to=(str(p["move_to"]) if p.get("move_to") is not None else None),
    )


def retry_operation(db: Session, op: SexingOfflineOperation) -> tuple[bool, str]:
    """Reintenta aplicar una operación pendiente vía apply_fish_save."""
    fish = resolve_fish_by_pit(db, op.pit)
    if fish is None:
        op.result_message = "PIT no encontrado."
        db.commit()
        return False, op.result_message
    res = _apply_op_payload(db, op, fish.id)
    op.fish_id = fish.id
    op.result_message = (res.message or "")[:255]
    if res.ok:
        op.status = "applied"
        op.applied_at = datetime.now()
    db.commit()
    return res.ok, res.message


def revive_and_retry(db: Session, op: SexingOfflineOperation) -> tuple[bool, str]:
    """Revive un pez declarado muerto/faena por error y reintenta la operación."""
    fish = resolve_fish_by_pit(db, op.pit)
    if fish is None:
        op.result_message = "PIT no encontrado."
        db.commit()
        return False, op.result_message
    if fish.state in ("dead", "faena"):
        fish.state = "alive"
        fish.date_of_death = None
        fish.depuration_start_time = None
        fish.updated_at = datetime.now()
        db.commit()
    return retry_operation(db, op)


def dismiss_operation(db: Session, op: SexingOfflineOperation, note: str = "") -> None:
    op.status = "dismissed"
    op.result_message = ("Descartada por supervisor. " + (note or ""))[:255]
    db.commit()


def apply_untagged_move(db: Session, source_pond_id: int, lot_id: int,
                        quantity: int, dest_pond_id: int) -> tuple[bool, str]:
    """Mueve N peces SIN marca de un lote de la fuente a un destino (movimiento
    agregado). Reutiliza el ajuste de biomasa/cachés del flujo online."""
    from app.api.views import _adjust_biomass_on_movement, _refresh_pond_runtime_cache_many
    if not lot_id or not quantity or int(quantity) <= 0:
        return False, "Lote o cantidad inválidos."
    lot_id, quantity = int(lot_id), int(quantity)
    avail = {b["lot_id"]: b["quantity"] for b in _untagged_balances(db, source_pond_id)}.get(lot_id, 0)
    if quantity > avail:
        return False, f"Saldo insuficiente en la fuente (disponible {avail})."
    # no mezclar lotes sin marca distintos en el destino
    dest_bal = {b["lot_id"]: b["quantity"] for b in _untagged_balances(db, dest_pond_id)}
    other = [l for l, q in dest_bal.items() if l != lot_id and q > 0]
    if other:
        return False, f"El destino ya tiene peces sin marca de otro lote {sorted(other)}."
    now = datetime.now()
    mov = PondMovement(fish_id=None, lot_id=lot_id, source_pond_id=source_pond_id,
                       destiny_pond_id=dest_pond_id, fish_quantity=quantity,
                       movement_reason="pond_movement", movement_time=now,
                       created_at=now, updated_at=now)
    db.add(mov)
    _adjust_biomass_on_movement(mov, db)
    _refresh_pond_runtime_cache_many([source_pond_id, dest_pond_id], db)
    db.commit()
    return True, f"Movidos {quantity} peces sin marca (lote {lot_id})."


def locked_pond_ids(db: Session) -> set[int]:
    out: set[int] = set()
    for s in active_sessions(db):
        for pid in (s.pond_ids or []):
            out.add(int(pid))
    return out


def pond_lock_session(db: Session, pond_id: int) -> Optional[SexadoOfflineSession]:
    """Devuelve la sesión activa que bloquea el estanque, o None."""
    for s in active_sessions(db):
        if pond_id in [int(p) for p in (s.pond_ids or [])]:
            return s
    return None


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------
def create_session(db: Session, source_pond_id: int, destination_pond_ids: list[int],
                   operator_id: Optional[int] = None) -> tuple[Optional[SexadoOfflineSession], Optional[str]]:
    """Crea una sesión bloqueando fuente + destinos. Devuelve (sesión, error)."""
    dests = []
    for pid in destination_pond_ids or []:
        pid = int(pid)
        if pid != source_pond_id and pid not in dests:
            dests.append(pid)
    if len(dests) > MAX_DESTINATIONS:
        return None, f"Máximo {MAX_DESTINATIONS} estanques destino."

    source = db.query(Pond).filter(Pond.id == source_pond_id).first()
    if not source:
        return None, "Estanque fuente no existe."
    if source.state == "inactive":
        return None, "Estanque fuente inactivo."

    for pid in dests:
        p = db.query(Pond).filter(Pond.id == pid).first()
        if not p:
            return None, f"Estanque destino {pid} no existe."
        if p.state == "inactive":
            return None, f"Estanque destino {p.name} inactivo."

    all_ids = [source_pond_id] + dests
    locked = locked_pond_ids(db)
    clash = [pid for pid in all_ids if pid in locked]
    if clash:
        return None, f"Estanque(s) ya en sesión offline: {sorted(clash)}."

    now = datetime.now()
    session = SexadoOfflineSession(
        token=uuid.uuid4().hex,
        operator_id=operator_id,
        source_pond_id=source_pond_id,
        pond_ids=all_ids,
        status="active",
        created_at=now,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session, None


def release_session(db: Session, token: str, status: str = "released") -> bool:
    session = db.query(SexadoOfflineSession).filter(SexadoOfflineSession.token == token,
                                                    SexadoOfflineSession.status == "active").first()
    if not session:
        return False
    session.status = status
    session.released_at = datetime.now()
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
def _fish_in_pond(db: Session, pond_id: int):
    """Peces con PIT actualmente en el estanque (por posición = último movimiento)."""
    rn = func.row_number().over(
        partition_by=PondMovement.fish_id,
        order_by=(PondMovement.movement_time.desc(), PondMovement.id.desc()),
    ).label("rn")
    sub = (
        db.query(PondMovement.fish_id.label("fid"),
                 PondMovement.destiny_pond_id.label("dest"), rn)
        .filter(PondMovement.fish_id.isnot(None))
        .subquery()
    )
    fids = db.query(sub.c.fid).filter(sub.c.rn == 1, sub.c.dest == pond_id)
    return (
        db.query(Fish)
        .filter(Fish.id.in_(fids))
        .filter(Fish.state.in_(["alive", "depuration"]))
        .filter(Fish.internal_id.isnot(None))
        .order_by(Fish.internal_id)
        .all()
    )


def _untagged_balances(db: Session, pond_id: int) -> list[dict]:
    incoming = dict(
        db.query(PondMovement.lot_id, func.coalesce(func.sum(PondMovement.fish_quantity), 0))
        .filter(PondMovement.fish_id.is_(None), PondMovement.destiny_pond_id == pond_id,
                PondMovement.lot_id.isnot(None))
        .group_by(PondMovement.lot_id).all()
    )
    outgoing = dict(
        db.query(PondMovement.lot_id, func.coalesce(func.sum(PondMovement.fish_quantity), 0))
        .filter(PondMovement.fish_id.is_(None), PondMovement.source_pond_id == pond_id,
                PondMovement.lot_id.isnot(None))
        .group_by(PondMovement.lot_id).all()
    )
    balances = {}
    for lot_id, q in incoming.items():
        balances[int(lot_id)] = int(q or 0)
    for lot_id, q in outgoing.items():
        balances[int(lot_id)] = balances.get(int(lot_id), 0) - int(q or 0)
    result = []
    for lot_id, qty in balances.items():
        if qty > 0:
            lot = db.query(Lot).filter(Lot.id == lot_id).first()
            result.append({"lot_id": lot_id,
                           "lot_label": (lot.internal_id if lot and lot.internal_id else str(lot_id)),
                           "quantity": qty})
    return result


def build_source_snapshot(db: Session, session: SexadoOfflineSession) -> dict:
    source = db.query(Pond).filter(Pond.id == session.source_pond_id).first()
    dest_ids = [int(p) for p in session.pond_ids if int(p) != session.source_pond_id]
    dest_ponds = db.query(Pond).filter(Pond.id.in_(dest_ids)).all() if dest_ids else []

    fish = _fish_in_pond(db, session.source_pond_id)
    fish_ids = [f.id for f in fish]
    latest = {}
    if fish_ids:
        for s in (db.query(FishSampling)
                  .filter(FishSampling.fish_id.in_(fish_ids))
                  .order_by(FishSampling.fish_id,
                            func.coalesce(FishSampling.registry_time, FishSampling.created_at).desc(),
                            FishSampling.id.desc()).all()):
            latest.setdefault(s.fish_id, s)

    def fish_dto(f):
        s = latest.get(f.id)
        return {
            "id": f.id, "pit": f.internal_id, "sex": (f.sex or "").lower() or None,
            "state": f.state,
            "development_state": (str(s.development_state) if s and s.development_state is not None else None),
            "weight": (float(s.weight) if s and s.weight is not None else None),
            "diameter": (float(s.diameter) if s and s.diameter is not None else None),
        }

    return {
        "token": session.token,
        "session_id": session.id,
        "server_time": datetime.now().isoformat(),
        "source": {"id": source.id, "name": source.name, "depuration": bool(source.depuration)},
        "destinations": [{"id": p.id, "name": p.name, "depuration": bool(p.depuration)} for p in dest_ponds],
        "fish": [fish_dto(f) for f in fish],
        "untagged_balances": _untagged_balances(db, session.source_pond_id),
        "dev_state_rules": VALID_DEV_STATES,
    }
