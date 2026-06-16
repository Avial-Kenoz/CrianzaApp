"""
Reconstruye biomass_checkpoints de tipo 'month_start' para todos los meses
desde el primer movimiento registrado hasta el mes actual.

Idempotente: borra los registros existentes de cada mes antes de re-insertarlos.

Uso:
    cd C:/Users/admin-server/Desktop/CrianzaApp
    python -m scripts.reconstruct_biomass_checkpoints
"""
import sys
import os

# Cargar el .env de CrianzaApp antes de importar session.py (que llama load_dotenv)
_CRIANZA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _CRIANZA_ROOT)

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(_CRIANZA_ROOT, ".env"), override=True)

from datetime import datetime, date, timedelta
from sqlalchemy import func

from app.db.session import SessionLocal
from app.models.ponds_movements import PondMovement
from app.models.biomass_checkpoint import BiomassCheckpoint
from app.api.views import (
    _sum_pond_lot_balance_until,
    _build_pond_lot_weight_snapshot,
    _build_lot_weight_snapshot,
    _save_biomass_checkpoint,
)


def run():
    db = SessionLocal()
    try:
        earliest = db.query(func.min(PondMovement.movement_time)).scalar()
        if not earliest:
            print("Sin movimientos en la base de datos. Nada que reconstruir.")
            return

        first_month = earliest.date().replace(day=1)
        current_month = date.today().replace(day=1)

        print(f"Reconstruyendo checkpoints desde {first_month} hasta {current_month}...")
        month = first_month
        total_pairs = 0

        while month <= current_month:
            snapshot_dt = datetime(month.year, month.month, 1)
            pond_lot_counts = _sum_pond_lot_balance_until(snapshot_dt, db)
            active = {k: v for k, v in pond_lot_counts.items() if v > 0}

            if not active:
                print(f"  {month.strftime('%Y-%m')}: sin peces activos, skip")
                month = (month + timedelta(days=32)).replace(day=1)
                continue

            lot_ids = {lid for _, lid in active}
            pond_lot_weights = _build_pond_lot_weight_snapshot(set(active.keys()), snapshot_dt, db)
            lot_fallback = _build_lot_weight_snapshot(lot_ids, snapshot_dt, db)

            pond_lot_biomass: dict = {}
            pond_lot_w_used: dict = {}
            for (pid, lid), qty in active.items():
                w = pond_lot_weights.get((pid, lid)) or lot_fallback.get(lid, 0.0)
                pond_lot_biomass[(pid, lid)] = qty * float(w or 0.0) / 1000.0
                pond_lot_w_used[(pid, lid)] = w

            # Idempotente: borrar checkpoints previos de este mes/tipo
            deleted = (
                db.query(BiomassCheckpoint)
                .filter_by(checkpoint_date=month, checkpoint_type="month_start")
                .delete()
            )

            _save_biomass_checkpoint(
                checkpoint_date=month,
                checkpoint_type="month_start",
                pond_lot_biomass=pond_lot_biomass,
                pond_lot_counts=active,
                pond_lot_weights=pond_lot_w_used,
                source_session_id=None,
                db=db,
            )
            db.commit()

            n = len(pond_lot_biomass)
            total_biomass = sum(pond_lot_biomass.values())
            repl = f" [reemplazo {deleted}]" if deleted else ""
            print(f"  OK {month.strftime('%Y-%m')}: {n} pares (pond,lot), {total_biomass:,.1f} kg totales{repl}")
            total_pairs += n
            month = (month + timedelta(days=32)).replace(day=1)

        print(f"\nReconstrucción completa. Total pares procesados: {total_pairs}")

    except Exception as exc:
        db.rollback()
        print(f"\nERROR: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run()
