"""
Reconstruye biomass_current por estanque aplicando la lógica de ajuste
sobre los movimientos ocurridos DESPUÉS del último muestreo cerrado.

Punto de partida por estanque:
  - biomass_measured   → foto del último muestreo cerrado
  - Si no hay muestreo cerrado → mantiene el valor actual (no toca)

Cada movimiento posterior al muestreo ajusta el total acumulado con la
misma lógica que _adjust_biomass_on_movement:
  - Tagged  → peso individual estimado (último FishSampling o avg del lote)
  - Untagged → N × avg_weight del estanque origen en pond_lot_stats

Al finalizar asigna el total acumulado a biomass_current.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, or_

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.views import _get_fish_weight_estimate, _get_lot_weight_estimate
from app.db.session import SessionLocal
from app.models.fish import Fish
from app.models.ponds import Pond
from app.models.ponds_movements import PondMovement
from app.models.sampling_sessions import SamplingSession


def main() -> None:
    db = SessionLocal()
    try:
        # Última sesión cerrada por estanque
        last_closed: dict[int, object] = dict(
            db.query(SamplingSession.pond_id, func.max(SamplingSession.closed_at))
            .filter(SamplingSession.closed_at.isnot(None))
            .group_by(SamplingSession.pond_id)
            .all()
        )

        ponds = db.query(Pond).order_by(Pond.id).all()

        changed = 0
        unchanged = 0
        skipped = 0
        results = []

        for pond in ponds:
            baseline_date = last_closed.get(pond.id)
            if baseline_date is None:
                skipped += 1
                continue

            if pond.biomass_measured is None:
                skipped += 1
                continue

            # Punto de partida: foto del muestreo
            running = Decimal(str(pond.biomass_measured))

            # Movimientos posteriores al último muestreo, orden cronológico
            movements = (
                db.query(PondMovement)
                .filter(
                    or_(
                        PondMovement.source_pond_id == pond.id,
                        PondMovement.destiny_pond_id == pond.id,
                    ),
                    PondMovement.movement_time > baseline_date,
                )
                .order_by(PondMovement.movement_time, PondMovement.id)
                .all()
            )

            for mov in movements:
                if mov.fish_id:
                    fish = db.query(Fish).filter(Fish.id == mov.fish_id).first()
                    lot_id = fish.lot_id if fish else mov.lot_id
                    peso_g = _get_fish_weight_estimate(mov.fish_id, lot_id, db)
                    if peso_g is None:
                        continue
                    delta_kg = Decimal(str(peso_g)) / 1000
                else:
                    qty = int(mov.fish_quantity or 1)
                    if qty <= 0:
                        continue
                    ref_pond = mov.source_pond_id if mov.source_pond_id else mov.destiny_pond_id
                    avg_g = _get_lot_weight_estimate(ref_pond, mov.lot_id, db)
                    if avg_g is None:
                        continue
                    delta_kg = Decimal(str(avg_g)) * qty / 1000

                if mov.source_pond_id == pond.id:
                    running = max(Decimal("0"), running - delta_kg)
                elif mov.destiny_pond_id == pond.id:
                    running += delta_kg

            new_val = float(running)
            old_val = float(pond.biomass_current) if pond.biomass_current is not None else 0.0
            diff = round(new_val - float(pond.biomass_measured), 3)

            pond.biomass_current = running
            pond.updated_at = datetime.now(UTC)

            results.append({
                "name": pond.name,
                "biomass_measured": float(pond.biomass_measured),
                "biomass_current_new": new_val,
                "diff_kg": diff,
                "movements_replayed": len(movements),
                "last_sampling": str(baseline_date)[:10],
            })

            if abs(new_val - old_val) > 0.001:
                changed += 1
            else:
                unchanged += 1

        db.commit()

        # Reporte
        results.sort(key=lambda r: abs(r["diff_kg"]), reverse=True)
        print(f"\n{'POND':<26} {'MEDIDA (kg)':>12} {'ACTUAL (kg)':>12} {'DIFF (kg)':>12} {'MOVS':>6} {'DESDE':>12}")
        print("-" * 82)
        for r in results[:30]:
            print(
                f"{r['name']:<26} "
                f"{r['biomass_measured']:>12.1f} "
                f"{r['biomass_current_new']:>12.1f} "
                f"{r['diff_kg']:>+12.1f} "
                f"{r['movements_replayed']:>6} "
                f"{r['last_sampling']:>12}"
            )
        if len(results) > 30:
            print(f"  ... y {len(results)-30} estanques más")

        print()
        print(f"changed={changed}  unchanged={unchanged}  skipped(no sampling)={skipped}")
        total_current = sum(r["biomass_current_new"] for r in results)
        total_measured = sum(r["biomass_measured"] for r in results)
        print(f"total_biomass_measured_kg={total_measured:.1f}")
        print(f"total_biomass_current_kg={total_current:.1f}")
        print(f"total_diff_kg={total_current - total_measured:+.1f}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
