from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
from app.db.session import SessionLocal
from app.models.ponds_movements import PondMovement
from app.schemas.ponds_movements import PondMovementCreate, PondMovementRead

router = APIRouter(prefix="/movements", tags=["movements"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_unregistered_balances_by_lot(db: Session, pond_id: int) -> dict[int, int]:
    """Saldo actual de peces sin PIT tag por lote en un estanque."""
    incoming_rows = (
        db.query(PondMovement.lot_id, func.coalesce(func.sum(PondMovement.fish_quantity), 0))
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.destiny_pond_id == pond_id,
            PondMovement.lot_id.isnot(None),
        )
        .group_by(PondMovement.lot_id)
        .all()
    )

    outgoing_rows = (
        db.query(PondMovement.lot_id, func.coalesce(func.sum(PondMovement.fish_quantity), 0))
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.source_pond_id == pond_id,
            PondMovement.lot_id.isnot(None),
        )
        .group_by(PondMovement.lot_id)
        .all()
    )

    balances: dict[int, int] = {}
    for lot_id, qty in incoming_rows:
        balances[int(lot_id)] = int(qty or 0)
    for lot_id, qty in outgoing_rows:
        balances[int(lot_id)] = balances.get(int(lot_id), 0) - int(qty or 0)

    return {lot_id: qty for lot_id, qty in balances.items() if qty > 0}

@router.get("/", response_model=List[PondMovementRead])
def list_movements(
    db: Session = Depends(get_db),
    from_: Optional[str] = None,
    to: Optional[str] = None,
    lot_id: Optional[int] = None,
    pond_id: Optional[int] = None,
    reason: Optional[str] = None
):
    query = db.query(PondMovement)
    if from_:
        query = query.filter(PondMovement.movement_time >= from_)
    if to:
        query = query.filter(PondMovement.movement_time <= to)
    if lot_id:
        query = query.filter(PondMovement.lot_id == lot_id)
    if pond_id:
        query = query.filter((PondMovement.source_pond_id == pond_id) | (PondMovement.destiny_pond_id == pond_id))
    if reason:
        query = query.filter(PondMovement.movement_reason == reason)
    return query.all()

@router.post("/", response_model=PondMovementRead)
def create_movement(movement: PondMovementCreate, db: Session = Depends(get_db)):
    # Bloqueo por sesión de sexado offline (fuente o destino en sesión)
    from app.services.sexado_sessions import pond_lock_session
    for pid in (movement.source_pond_id, movement.destiny_pond_id):
        if pid is not None and pond_lock_session(db, pid):
            raise HTTPException(
                status_code=409,
                detail=f"Pond {pid} está en sesión de sexado offline (solo lectura hasta sincronizar).",
            )

    # Validaciones de negocio
    from app.models.ponds import Pond
    for pond_field, pond_id in (
        ("source_pond_id", movement.source_pond_id),
        ("destiny_pond_id", movement.destiny_pond_id),
    ):
        if pond_id is not None:
            p = db.query(Pond.state).filter(Pond.id == pond_id).first()
            if p and p.state == "inactive":
                raise HTTPException(
                    status_code=400,
                    detail=f"Pond {pond_id} is inactive and cannot be used in movements.",
                )

    allowed_reasons = [
        "mortality", "depuration", "inventory_mismatch", "registration", "devious",
        "first_load", "pond_movement", "unmarked_devious", "missing_number", "faena",
        "reconciliation"
    ]
    if movement.movement_reason not in allowed_reasons:
        raise HTTPException(status_code=400, detail="Invalid movement_reason")
    if movement.fish_quantity <= 0:
        raise HTTPException(status_code=400, detail="fish_quantity must be positive")

    # Validación de posición del pez con PIT tag
    if movement.fish_id is not None and movement.source_pond_id is not None:
        from app.models.fish import Fish
        from app.models.ponds_movements import PondMovement as PM2
        from sqlalchemy import desc
        last_mov = (
            db.query(PM2)
            .filter(PM2.fish_id == movement.fish_id)
            .order_by(desc(PM2.movement_time), desc(PM2.id))
            .first()
        )
        if last_mov and last_mov.destiny_pond_id != movement.source_pond_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Fish {movement.fish_id} is not in pond {movement.source_pond_id}. "
                    f"Last movement placed it in pond {last_mov.destiny_pond_id}."
                ),
            )
        if last_mov and movement.movement_time and movement.movement_time < last_mov.movement_time:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Movement time {movement.movement_time} is earlier than fish {movement.fish_id}'s "
                    f"last movement at {last_mov.movement_time}. Backdated movements must use DB corrections."
                ),
            )

    # Regla de negocio: no mezclar lotes de peces sin PIT tag en un estanque.
    # Excepcion: peces con PIT tag (movement.fish_id) si pueden coexistir en otro lote.
    if movement.fish_id is None and movement.destiny_pond_id is not None:
        if movement.lot_id is None:
            raise HTTPException(
                status_code=400,
                detail="lot_id is required when fish_id is null (unregistered fish)",
            )

        balances = _get_unregistered_balances_by_lot(db, movement.destiny_pond_id)
        active_unregistered_lots = set(balances.keys())

        if active_unregistered_lots and movement.lot_id not in active_unregistered_lots:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Validation error: destination pond already has unregistered fish "
                    f"assigned to lot(s) {sorted(active_unregistered_lots)}. "
                    "Cannot mix unregistered fish from a different lot. "
                    "Register fish with PIT tag first or use the same lot."
                ),
            )

    db_movement = PondMovement(**movement.dict())
    db.add(db_movement)
    db.commit()
    db.refresh(db_movement)

    # Lógica de estado de pez según movimiento de depuración
    if movement.fish_id:
        from app.models.fish import Fish
        fish = db.query(Fish).filter(Fish.id == movement.fish_id).first()
        if fish:
            # Si entra a estanque de depuración, estado = depuration
            if movement.movement_reason == "depuration" and movement.destiny_pond_id:
                from app.models.ponds import Pond
                pond = db.query(Pond).filter(Pond.id == movement.destiny_pond_id).first()
                if pond and pond.depuration:
                    fish.state = "depuration"
                    db.commit()
            # Si sale de depuración a estanque normal, estado = alive
            if movement.movement_reason == "pond_movement" and movement.source_pond_id:
                from app.models.ponds import Pond
                pond = db.query(Pond).filter(Pond.id == movement.source_pond_id).first()
                if pond and pond.depuration:
                    fish.state = "alive"
                    db.commit()

    return db_movement
