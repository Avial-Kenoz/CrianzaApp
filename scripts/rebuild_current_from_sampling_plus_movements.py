from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.views import _get_fish_weight_estimate, _get_lot_weight_estimate
from app.db.session import SessionLocal
from app.models.fish import Fish
from app.models.ponds import Pond
from app.models.ponds_movements import PondMovement
from app.models.sampling_sessions import SamplingSession


def _to_decimal(value: Decimal | float | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def main() -> None:
    db = SessionLocal()
    try:
        # 1) Última fecha de cierre de muestreo por estanque
        last_sampling_rows = (
            db.query(SamplingSession.pond_id, func.max(SamplingSession.closed_at))
            .filter(SamplingSession.closed_at.isnot(None))
            .group_by(SamplingSession.pond_id)
            .all()
        )
        last_sampling_by_pond = {int(pid): closed_at for pid, closed_at in last_sampling_rows}

        # 2) Inicializar biomasa actual desde biomasa medida (snapshot)
        ponds = db.query(Pond).all()
        initialized = 0
        for pond in ponds:
            if pond.biomass_measured is not None:
                pond.biomass_current = pond.biomass_measured
                pond.updated_at = datetime.utcnow()
                initialized += 1

        db.flush()

        # 3) Reprocesar movimientos en orden temporal, aplicando solo al lado
        #    del estanque cuya fecha de muestreo ya había ocurrido.
        movements = (
            db.query(PondMovement)
            .order_by(PondMovement.movement_time.asc(), PondMovement.id.asc())
            .all()
        )

        moved_count = 0
        for mov in movements:
            if not mov.movement_time:
                continue

            if mov.fish_id:
                fish = db.query(Fish).filter(Fish.id == mov.fish_id).first()
                lot_id = fish.lot_id if fish else mov.lot_id
                peso_g = _get_fish_weight_estimate(mov.fish_id, lot_id, db)
                if peso_g is None:
                    continue
                delta_kg = Decimal(str(peso_g)) / 1000
            else:
                qty = int(mov.fish_quantity or 0)
                if qty <= 0:
                    continue
                ref_pond_id = mov.source_pond_id if mov.source_pond_id else mov.destiny_pond_id
                avg_g = _get_lot_weight_estimate(ref_pond_id, mov.lot_id, db)
                if avg_g is None:
                    continue
                delta_kg = Decimal(str(avg_g)) * qty / 1000

            # Source side
            if mov.source_pond_id:
                src_cutoff = last_sampling_by_pond.get(int(mov.source_pond_id))
                if src_cutoff and mov.movement_time > src_cutoff:
                    src = db.query(Pond).filter(Pond.id == mov.source_pond_id).first()
                    if src and src.biomass_current is not None:
                        src.biomass_current = max(Decimal("0"), _to_decimal(src.biomass_current) - delta_kg)
                        src.updated_at = datetime.utcnow()
                        moved_count += 1

            # Destiny side
            if mov.destiny_pond_id:
                dst_cutoff = last_sampling_by_pond.get(int(mov.destiny_pond_id))
                if dst_cutoff and mov.movement_time > dst_cutoff:
                    dst = db.query(Pond).filter(Pond.id == mov.destiny_pond_id).first()
                    if dst and dst.biomass_current is not None:
                        dst.biomass_current = _to_decimal(dst.biomass_current) + delta_kg
                        dst.updated_at = datetime.utcnow()
                        moved_count += 1

        db.commit()

        equal_count = db.query(func.count(Pond.id)).filter(
            func.round(func.coalesce(Pond.biomass_current, 0), 3)
            == func.round(func.coalesce(Pond.biomass_measured, 0), 3)
        ).scalar()
        total_count = db.query(func.count(Pond.id)).scalar()
        diff_count = int(total_count) - int(equal_count)
        negatives = db.query(func.count(Pond.id)).filter(func.coalesce(Pond.biomass_current, 0) < 0).scalar()

        print(f"initialized_from_snapshot={initialized}")
        print(f"movements_processed={len(movements)}")
        print(f"side_adjustments_applied={moved_count}")
        print(f"ponds_equal_current_vs_measured={int(equal_count)}")
        print(f"ponds_different_current_vs_measured={diff_count}")
        print(f"ponds_negative_current={int(negatives)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
