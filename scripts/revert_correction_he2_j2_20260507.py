"""
Reversion de 18 movimientos de correccion excedentes HE2->J2 del 2026-05-07.

Batches A y B (ids 168241-168258) corrieron antes de que llegara ningun pez
Iberia/Fatima a HE2, dejando HE2 con net negativo (-12 Iberia, -6 Fatima)
y J2 con 18 peces de mas en sus movimientos vs el cache real.

Se crean 18 movimientos inversos J2->HE2 con reason=correction_revert para
anular el efecto contable sin eliminar registros existentes.
"""

import os, sys
from datetime import datetime, timezone
from sqlalchemy import create_engine, text

DB_URL = "postgresql+psycopg://fastapp_user:fastapp_pass@localhost/fastapp_etapa1"
engine = create_engine(DB_URL)

HE2 = 38
J2  = 45
TARGET_IDS = list(range(168241, 168259))  # 168241..168258 — batches A y B
REVERT_REASON = "correction_revert"
REVERT_NOTE   = "Anula batches A+B del 2026-05-07 18:02 (correcciones previas a llegada de peces)"

def run(dry_run: bool = True):
    db = engine.connect()
    try:
        # Verificar que los movimientos objetivo existen y tienen el perfil esperado
        rows = db.execute(text("""
            SELECT pm.id, pm.movement_time, pm.movement_reason,
                   pm.fish_quantity, pm.lot_id,
                   sp.internal_id src, dp.internal_id dst
            FROM ponds_movements pm
            LEFT JOIN ponds sp ON sp.id = pm.source_pond_id
            LEFT JOIN ponds dp ON dp.id = pm.destiny_pond_id
            WHERE pm.id = ANY(:ids)
            ORDER BY pm.id
        """), {"ids": TARGET_IDS}).fetchall()

        print(f"\n{'='*65}")
        print(f"  {'DRY RUN' if dry_run else 'EJECUTANDO'} — reversion de {len(rows)} movimientos")
        print(f"{'='*65}")

        errors = []
        for m in rows:
            if m.src != "HE2" or m.dst != "J2":
                errors.append(f"  ERROR id={m.id}: esperado HE2->J2, encontrado {m.src}->{m.dst}")
            if m.movement_reason not in ("correction",):
                errors.append(f"  AVISO id={m.id}: reason={m.movement_reason} (distinto de 'correction')")

        if errors:
            print("\n  Problemas detectados:")
            for e in errors: print(e)
            print("\n  Abortando.")
            return

        print(f"\n  Movimientos a revertir:")
        from collections import defaultdict
        by_lot = defaultdict(int)
        for m in rows:
            by_lot[m.lot_id] += m.fish_quantity
            print(f"    id={m.id}  {str(m.movement_time)[:19]}  lot={m.lot_id}  {m.src}->{m.dst}")

        print(f"\n  Resumen por lote: {dict(by_lot)}")
        print(f"  Total peces: {sum(by_lot.values())}")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        to_insert = [
            {
                "source_pond_id":  J2,
                "destiny_pond_id": HE2,
                "fish_quantity":   m.fish_quantity,
                "lot_id":          m.lot_id,
                "movement_reason": REVERT_REASON,
                "movement_time":   now,
                "created_at":      now,
                "updated_at":      now,
            }
            for m in rows
        ]

        print(f"\n  Movimientos a insertar: {len(to_insert)} (J2 -> HE2, reason={REVERT_REASON})")

        if dry_run:
            print("\n  [DRY RUN] Sin cambios. Ejecutar con dry_run=False para aplicar.")
            return

        # Insertar
        db.execute(text("""
            INSERT INTO ponds_movements
                (source_pond_id, destiny_pond_id, fish_quantity, lot_id,
                 movement_reason, movement_time, created_at, updated_at)
            VALUES
                (:source_pond_id, :destiny_pond_id, :fish_quantity, :lot_id,
                 :movement_reason, :movement_time, :created_at, :updated_at)
        """), to_insert)
        db.commit()
        db.execute(text("SELECT pg_sleep(0)"))  # flush
        print(f"\n  OK — {len(to_insert)} movimientos insertados.")

        # Verificar net resultante
        print("\n  Verificacion net post-reversion:")
        for pond_id, label in [(HE2, "HE2"), (J2, "J2")]:
            for lot_id in [3, 4]:
                r = db.execute(text("""
                    WITH e AS (SELECT COALESCE(SUM(fish_quantity),0) qty FROM ponds_movements
                               WHERE destiny_pond_id=:pid AND lot_id=:lot),
                         x AS (SELECT COALESCE(SUM(fish_quantity),0) qty FROM ponds_movements
                               WHERE source_pond_id=:pid AND lot_id=:lot)
                    SELECT e.qty - x.qty AS net FROM e, x
                """), {"pid": pond_id, "lot": lot_id}).fetchone()
                print(f"    {label} lot {lot_id}: net={int(r.net)}")

    except Exception as e:
        db.rollback()
        print(f"\n  ERROR: {e}")
        raise
    finally:
        db.close()



if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Aplicar cambios (sin este flag es dry-run)")
    args = p.parse_args()
    run(dry_run=not args.apply)
