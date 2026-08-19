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


def session_by_token_any(db: Session, token: str) -> Optional[SexadoOfflineSession]:
    """La sesión del token sin filtrar por estado. La usan los endpoints de
    conflictos: si el tablet reintenta un resolve tras un corte de red, tiene que
    poder enterarse de que la sesión ya cerró en vez de comerse un 404 ciego."""
    return db.query(SexadoOfflineSession).filter(
        SexadoOfflineSession.token == token).first()


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


def finalize_session(db: Session, session: SexadoOfflineSession, has_pending: bool = False) -> None:
    """Tras sincronizar: si quedan contingencias, la sesión pasa a 'reconciling'
    (sigue bloqueando); si todo quedó limpio, 'synced' y libera el bloqueo.

    Mira los pendientes REALES de la sesión, no solo los del batch que acaba de
    entrar: un segundo sync sin contingencias nuevas no puede cerrar una sesión
    que todavía tiene operaciones esperando en la bandeja (pasó el 19/08/2026 y
    dejó una pendiente invisible). `has_pending` queda como piso por si el
    llamador ya sabe de pendientes que aún no commiteó.
    """
    if has_pending or pending_count(db, session.id) > 0:
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


def dismiss_operation(db: Session, op: SexingOfflineOperation, note: str = "",
                      by: str = "supervisor") -> None:
    who = "Descartada en terreno." if by == "field" else "Descartada por supervisor."
    op.status = "dismissed"
    op.result_message = (who + " " + (note or "")).strip()[:255]
    db.commit()


# ---------------------------------------------------------------------------
# Clasificación de conflictos: qué pierde el operador si descarta
# ---------------------------------------------------------------------------
# El tablet ahora puede resolver conflictos en terreno (PR-S3), así que necesita
# mostrar el costo del descarte. La severidad NO se congela al sincronizar: se
# calcula al listar, porque distinguir un re-escaneo inocuo de una medición que
# se pierde exige comparar la captura contra el pez tal como está hoy en la base.

SEV_DUPLICATE = "duplicate"        # descartar no pierde nada
SEV_DATA = "data"                  # se pierde una medición; el pez sigue registrado
SEV_TRACEABILITY = "traceability"  # se pierde el pez o se rompe la cadena de tags

# Motivos que el sync escribe en `reason_code` (códigos estables, no el mensaje).
REASON_CONTINGENCY = "contingency"
REASON_PIT_ACTIVE = "pit_active_on_live_fish"
REASON_PIT_REUSE_CONFIRM = "pit_needs_reuse_confirm"
REASON_LOST_TAG = "lost_tag_pending"
REASON_PIT_NOT_FOUND = "pit_not_found"
REASON_NO_BALANCE = "no_untagged_balance"
REASON_DEST_NOT_ALLOWED = "destination_not_allowed"
REASON_FISH_MOVED = "fish_moved_away"
REASON_FISH_NOT_ACTIVE = "fish_not_active"
REASON_INVALID_MEASUREMENT = "invalid_measurement"

_SEVERITY_BY_REASON = {
    REASON_CONTINGENCY: SEV_TRACEABILITY,
    REASON_PIT_REUSE_CONFIRM: SEV_TRACEABILITY,
    REASON_LOST_TAG: SEV_TRACEABILITY,
    REASON_PIT_NOT_FOUND: SEV_TRACEABILITY,
    REASON_NO_BALANCE: SEV_TRACEABILITY,
    REASON_FISH_NOT_ACTIVE: SEV_TRACEABILITY,
    REASON_DEST_NOT_ALLOWED: SEV_DATA,
    REASON_FISH_MOVED: SEV_DATA,
    REASON_INVALID_MEASUREMENT: SEV_DATA,
    # REASON_PIT_ACTIVE se decide comparando contra la base (ver abajo)
}

SEVERITY_COPY = {
    SEV_DUPLICATE: {
        "label": "Duplicado",
        "loss": "Nada: este pez ya está registrado con estos mismos datos.",
    },
    SEV_DATA: {
        "label": "Dato",
        "loss": "Se pierde esta medición. El pez sigue registrado y ubicado.",
    },
    SEV_TRACEABILITY: {
        "label": "Trazabilidad",
        "loss": "Este pez queda sin registro. Si no estás seguro, mandalo al supervisor.",
    },
}


# Mismos patrones que el backfill de la migración 20260819_01: si cambia la
# redacción de un mensaje en views.py, el código se corrige acá y en un solo lugar.
_REASON_PATTERNS = [
    ("contingencia", REASON_CONTINGENCY),
    ("activo en un pez vivo", REASON_PIT_ACTIVE),
    ("no se puede reutilizar", REASON_PIT_ACTIVE),
    ("confirmación de reutilización", REASON_PIT_REUSE_CONFIRM),
    ("tag perdido vigente", REASON_LOST_TAG),
    ("pit no encontrado", REASON_PIT_NOT_FOUND),
    ("no hay saldo", REASON_NO_BALANCE),
    ("destino no está entre los preconfigurados", REASON_DEST_NOT_ALLOWED),
    ("destino no válido", REASON_DEST_NOT_ALLOWED),
    ("destino debe ser distinto", REASON_DEST_NOT_ALLOWED),
    ("ya no está en este estanque", REASON_FISH_MOVED),
    ("no está activo", REASON_FISH_NOT_ACTIVE),
    ("peso debe ser mayor", REASON_INVALID_MEASUREMENT),
    ("diámetro debe ser mayor", REASON_INVALID_MEASUREMENT),
    ("estado de desarrollo", REASON_INVALID_MEASUREMENT),
    ("debe indicar sexo", REASON_INVALID_MEASUREMENT),
]


def reason_from_message(message: Optional[str]) -> Optional[str]:
    """Deriva el código de motivo del mensaje que devuelve apply_*. Las capas de
    aplicación hablan en español libre; esto es el traductor a código estable."""
    text = (message or "").lower()
    for needle, code in _REASON_PATTERNS:
        if needle in text:
            return code
    return None


def _num_eq(a, b, tol: float = 0.001) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _is_rescan(db: Session, op: SexingOfflineOperation, fish: Fish) -> bool:
    """¿La captura repite lo que el pez ya tiene? Entonces descartar no pierde nada."""
    p = op.payload or {}
    if (p.get("sex") or None) != (fish.sex or None):
        return False
    # mismo criterio de "último muestreo" que build_source_snapshot
    last = (db.query(FishSampling)
            .filter(FishSampling.fish_id == fish.id)
            .order_by(func.coalesce(FishSampling.registry_time, FishSampling.created_at).desc(),
                      FishSampling.id.desc())
            .first())
    if last is None:
        return False
    if (p.get("development_state") or None) != (last.development_state or None):
        return False
    if not _num_eq(p.get("weight"), last.weight):
        return False
    if not _num_eq(p.get("diameter"), last.diameter):
        return False
    # si además pedía mover, el pez ya tiene que estar en ese destino
    move_to = p.get("move_to")
    if move_to is not None and current_pond_id(db, fish.id) != int(move_to):
        return False
    return True


def current_pond_id(db: Session, fish_id: int) -> Optional[int]:
    """Estanque donde está el pez hoy (destino de su último movimiento)."""
    m = (db.query(PondMovement)
         .filter(PondMovement.fish_id == fish_id)
         .order_by(PondMovement.movement_time.desc(), PondMovement.id.desc())
         .first())
    return m.destiny_pond_id if m else None


def conflict_severity(db: Session, op: SexingOfflineOperation) -> str:
    """Severidad de un conflicto pendiente. Lo desconocido cae en trazabilidad:
    ante la duda, que el operador vea el aviso más fuerte."""
    code = op.reason_code
    if code == REASON_PIT_ACTIVE:
        fish = resolve_fish_by_pit(db, op.pit) if op.pit else None
        if fish is None:
            return SEV_TRACEABILITY
        return SEV_DUPLICATE if _is_rescan(db, op, fish) else SEV_DATA
    return _SEVERITY_BY_REASON.get(code, SEV_TRACEABILITY)


def conflict_row(db: Session, op: SexingOfflineOperation) -> dict:
    """Representación de un conflicto para el tablet (y para la bandeja)."""
    sev = conflict_severity(db, op)
    fish = resolve_fish_by_pit(db, op.pit) if op.pit else None
    return {
        "id": op.id,
        "pit": op.pit,
        "kind": op.kind,
        "reason_code": op.reason_code,
        "message": op.result_message,
        "payload": op.payload or {},
        "captured_at": op.captured_at.isoformat() if op.captured_at else None,
        "severity": sev,
        "severity_label": SEVERITY_COPY[sev]["label"],
        "severity_loss": SEVERITY_COPY[sev]["loss"],
        "fish_found": fish is not None,
        "fish_state": (fish.state if fish else None),
    }


# Acciones que el tablet puede ejecutar sobre un conflicto. `revive` (revivir un
# pez declarado muerto) queda deliberadamente fuera: es corrección de supervisor.
FIELD_ACTIONS = ("retry", "dismiss", "supervisor")


def resolve_conflict(db: Session, op: SexingOfflineOperation, action: str) -> tuple[bool, str]:
    """Resuelve un conflicto desde el tablet. 'supervisor' lo deja intacto en la
    bandeja: es la salida para lo que no se puede decidir en el estanque."""
    if action not in FIELD_ACTIONS:
        return False, f"Acción '{action}' no válida."
    if op.status != "pending_review":
        return False, f"La operación ya está '{op.status}'."
    if action == "supervisor":
        return True, "Queda para el supervisor."
    if action == "dismiss":
        dismiss_operation(db, op, by="field")
        return True, "Descartada."
    return retry_operation(db, op)


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


def release_session_by_id(db: Session, session_id: int, note: str = "") -> tuple[bool, str]:
    """Cierre desde la app (supervisor): libera el candado de una sesión que aún
    bloquea. Advertencia: el tablet pierde lo que no haya sincronizado, por eso
    la UI lo confirma explícitamente. Reversible con reactivate_session."""
    s = db.query(SexadoOfflineSession).filter(SexadoOfflineSession.id == session_id).first()
    if not s:
        return False, "Sesión no encontrada."
    if s.status not in LOCKING_STATUSES:
        return False, f"La sesión #{session_id} ya está '{s.status}'."
    s.status = "released"
    s.released_at = datetime.now()
    if note:
        s.notes = ((s.notes or "") + " " + note).strip()
    db.commit()
    return True, f"Estanques de la sesión #{session_id} liberados."


def reactivate_session(db: Session, session_id: int) -> tuple[bool, str]:
    """Red de seguridad: vuelve una sesión cerrada (released/synced) a 'active'
    para que un tablet con lecturas huérfanas pueda volver a sincronizar. Rechaza
    si sus estanques ya están tomados por otra sesión (evita doble bloqueo)."""
    s = db.query(SexadoOfflineSession).filter(SexadoOfflineSession.id == session_id).first()
    if not s:
        return False, "Sesión no encontrada."
    if s.status in LOCKING_STATUSES:
        return False, f"La sesión #{session_id} ya está activa."
    locked = locked_pond_ids(db)
    clash = [int(p) for p in (s.pond_ids or []) if int(p) in locked]
    if clash:
        return False, f"No se puede reactivar: estanques {sorted(clash)} ya están en otra sesión."
    s.status = "active"
    s.released_at = None
    db.commit()
    return True, f"Sesión #{session_id} reactivada. El tablet ya puede sincronizar."


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
