"""
Reconstruye biomass_checkpoints de tipo 'sampling' para TODAS las sesiones
históricas cerradas, usando mediciones reales (sampling_records) y conteos
de movimientos — sin depender de pond.biomass_current.

Fórmula por (pond_id, lot_id):
    avg_weight_g = mean(sampling_records.weight) para esa sesión y lote
    fish_count   = balance de ponds_movements en fecha de la sesión
    biomass_kg   = fish_count × avg_weight_g / 1000

Idempotente: UPSERT por (checkpoint_date, checkpoint_type, pond_id, lot_id).

Uso:
    cd C:/Users/admin-server/Desktop/CrianzaApp
    python -m scripts.reconstruct_all_sampling_checkpoints
"""
import sys
import os
from collections import defaultdict
from datetime import datetime, timedelta

_CRIANZA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _CRIANZA_ROOT)

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(_CRIANZA_ROOT, ".env"), override=True)

from app.db.session import SessionLocal
from app.models.sampling_sessions import SamplingSession
from app.models.sampling_records import SamplingRecord
from app.models.biomass_checkpoint import BiomassCheckpoint
from app.api.views import _sum_pond_lot_balance_until


def run():
    db = SessionLocal()
    try:
        sessions = (
            db.query(SamplingSession)
            .filter(SamplingSession.closed_at.isnot(None))
            .order_by(SamplingSession.registry_date)
            .all()
        )
        print(f"Sesiones cerradas a procesar: {len(sessions)}")

        upserted = 0
        skipped = 0

        for session in sessions:
            records = (
                db.query(SamplingRecord)
                .filter(
                    SamplingRecord.session_id == session.id,
                    SamplingRecord.lot_id.isnot(None),
                    SamplingRecord.weight.isnot(None),
                )
                .all()
            )
            if not records:
                skipped += 1
                continue

            # avg_weight_g por lote desde los registros de la sesión
            weights_by_lot: dict[int, list[float]] = defaultdict(list)
            for r in records:
                weights_by_lot[int(r.lot_id)].append(float(r.weight))

            # Conteos de movimientos hasta el momento de la sesión (inclusive)
            snapshot_dt = datetime.combine(session.registry_date, datetime.min.time()) + timedelta(seconds=1)
            pond_lot_counts = _sum_pond_lot_balance_until(snapshot_dt, db)

            now = datetime.utcnow()
            for lot_id, weights in weights_by_lot.items():
                avg_w = sum(weights) / len(weights)
                fish_count = int(pond_lot_counts.get((session.pond_id, lot_id), 0))
                biomass_kg = round(fish_count * avg_w / 1000.0, 3)

                existing = (
                    db.query(BiomassCheckpoint)
                    .filter_by(
                        checkpoint_date=session.registry_date,
                        checkpoint_type="sampling",
                        pond_id=session.pond_id,
                        lot_id=lot_id,
                    )
                    .first()
                )
                if existing:
                    existing.fish_count        = fish_count
                    existing.avg_weight_g      = round(avg_w, 3)
                    existing.biomass_kg        = biomass_kg
                    existing.source_session_id = session.id
                    existing.computed_at       = now
                else:
                    db.add(BiomassCheckpoint(
                        checkpoint_date=session.registry_date,
                        checkpoint_type="sampling",
                        pond_id=session.pond_id,
                        lot_id=lot_id,
                        fish_count=fish_count,
                        avg_weight_g=round(avg_w, 3),
                        biomass_kg=biomass_kg,
                        source_session_id=session.id,
                        computed_at=now,
                    ))
                upserted += 1

            # Flush por sesión para que el siguiente query vea los registros ya insertados
            # (evita UniqueViolation cuando dos sesiones del mismo día comparten pond/lot)
            db.flush()

        db.commit()
        print(f"Checkpoints sampling reconstruidos: {upserted}")
        print(f"Sesiones sin registros de peso (skip): {skipped}")
        print("Listo. Ejecutar reconstruct_biomass_checkpoints.py para actualizar month_start.")

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run()
