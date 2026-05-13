from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import func

# Allow direct execution: python scripts/recalc_biomass_baseline.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.views import _recalc_pond_biomass
from app.db.session import SessionLocal
from app.models.ponds import Pond


def main() -> None:
    db = SessionLocal()
    try:
        pond_ids = [pid for (pid,) in db.query(Pond.id).order_by(Pond.id).all()]
        for pond_id in pond_ids:
            _recalc_pond_biomass(pond_id, db)

        db.commit()

        ponds_count, total_current, total_measured = db.query(
            func.count(Pond.id),
            func.coalesce(func.sum(Pond.biomass_current), 0),
            func.coalesce(func.sum(Pond.biomass_measured), 0),
        ).one()

        print(f"recalculated_ponds={len(pond_ids)}")
        print(f"ponds_count={ponds_count}")
        print(f"total_biomass_current_kg={float(total_current):.3f}")
        print(f"total_biomass_measured_kg={float(total_measured):.3f}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
