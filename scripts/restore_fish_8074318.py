"""Repara el registro del pez 8074318 (id 50927), erróneamente reconciliado.

El pez se encontró vivo. La reconciliación del estanque 79 (2026-06-17 21:18:36)
lo sacó del sistema. Este script revierte EXACTAMENTE ese efecto:

  1) fish 50927          : state 'reconciled' -> 'alive', devious_time -> NULL
  2) ponds_movements     : borra la salida 171774 (reconciliation, destiny NULL)
  3) tag_detachment 406  : 'written_off'/'left_unregistered' -> 'retagged'/NULL/NULL
  4) ponds 79            : repone biomasa individual (+peso estimado) y recomputa
                           los contadores derivados vía _refresh_pond_runtime_cache

Todo en UNA transacción. Antes de tocar nada respalda las filas afectadas a JSON.

Uso:
  python scripts/restore_fish_8074318.py            # ejecuta
  python scripts/restore_fish_8074318.py --dry-run  # solo muestra antes/después
"""
from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from decimal import Decimal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from app.db.session import SessionLocal
from app.api.views import _refresh_pond_runtime_cache, _get_fish_weight_estimate

FISH_ID = 50927
TAG = "8074318"
MOVEMENT_ID = 171774
TAG_EVENT_ID = 406
POND_ID = 79
LOT_ID = 3


def _json_default(o):
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    return str(o)


def snapshot(db) -> dict:
    fish = db.execute(text("SELECT * FROM fish WHERE id=:i"), {"i": FISH_ID}).mappings().first()
    mov = db.execute(text("SELECT * FROM ponds_movements WHERE id=:i"), {"i": MOVEMENT_ID}).mappings().first()
    ev = db.execute(text("SELECT * FROM tag_detachment_events WHERE id=:i"), {"i": TAG_EVENT_ID}).mappings().first()
    pond = db.execute(text(
        "SELECT id,name,state,biomass_current,tagged_count,unregistered_count,"
        "n_fish_cached,active_lots_count,active_lot_ids,runtime_cache_updated_at,"
        "last_tag_reconciliation_at FROM ponds WHERE id=:i"
    ), {"i": POND_ID}).mappings().first()
    return {
        "fish": dict(fish) if fish else None,
        "ponds_movements_171774": dict(mov) if mov else None,
        "tag_detachment_events_406": dict(ev) if ev else None,
        "ponds_79": dict(pond) if pond else None,
    }


def show(title: str, snap: dict) -> None:
    print(f"\n===== {title} =====")
    f = snap["fish"]
    print(f"  fish.state={f and f['state']}  devious_time={f and f['devious_time']}")
    print(f"  movimiento 171774 existe: {snap['ponds_movements_171774'] is not None}")
    e = snap["tag_detachment_events_406"]
    print(f"  tag_event 406: status={e and e['status']} resolution={e and e['resolution']} resolved_at={e and e['resolved_at']}")
    p = snap["ponds_79"]
    print(f"  pond 79: tagged_count={p and p['tagged_count']} n_fish_cached={p and p['n_fish_cached']} biomass_current={p and p['biomass_current']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        before = snapshot(db)
        show("ANTES", before)

        # ── Validaciones de seguridad / idempotencia ──
        f = before["fish"]
        if f is None:
            raise SystemExit(f"ABORT: no existe fish id {FISH_ID}")
        if f["internal_id"] != TAG:
            raise SystemExit(f"ABORT: fish {FISH_ID} no tiene tag {TAG} (tiene {f['internal_id']})")
        if f["state"] != "reconciled":
            raise SystemExit(f"ABORT: fish {FISH_ID} no está en estado 'reconciled' (está '{f['state']}'). ¿Ya se reparó?")
        if before["ponds_movements_171774"] is None:
            raise SystemExit(f"ABORT: no existe el movimiento {MOVEMENT_ID}. ¿Ya se reparó?")

        # Peso estimado que la reconciliación restó del estanque (reversión exacta)
        peso_g = _get_fish_weight_estimate(FISH_ID, LOT_ID, db, pond_id=POND_ID)
        if not peso_g:
            raise SystemExit("ABORT: no se pudo estimar el peso del pez para reponer biomasa")
        delta_kg = Decimal(str(peso_g)) / Decimal("1000")
        print(f"\n  Reposición de biomasa estanque 79: +{delta_kg} kg (peso estimado {peso_g} g)")

        # ── Respaldo de filas afectadas ──
        backups_dir = PROJECT_ROOT / "scripts" / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backups_dir / f"restore_fish_{TAG}_{stamp}.json"
        backup_path.write_text(json.dumps(before, default=_json_default, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Respaldo escrito en: {backup_path}")

        if args.dry_run:
            print("\n[DRY-RUN] No se aplican cambios.")
            return

        now = datetime.utcnow()

        # 1) fish -> alive
        db.execute(text(
            "UPDATE fish SET state='alive', devious_time=NULL, updated_at=:n WHERE id=:i"
        ), {"n": now, "i": FISH_ID})

        # 2) borrar el movimiento de salida por reconciliación
        db.execute(text("DELETE FROM ponds_movements WHERE id=:i"), {"i": MOVEMENT_ID})

        # 3) revertir el evento de pérdida de tag a 'retagged' / sin resolver
        db.execute(text(
            "UPDATE tag_detachment_events "
            "SET status='retagged', resolution=NULL, resolved_at=NULL WHERE id=:i"
        ), {"i": TAG_EVENT_ID})

        db.flush()

        # 4a) reponer biomasa individual del estanque (único campo no derivado)
        db.execute(text(
            "UPDATE ponds SET biomass_current = COALESCE(biomass_current,0) + :d, updated_at=:n WHERE id=:i"
        ), {"d": delta_kg, "n": now, "i": POND_ID})

        # 4b) recomputar contadores derivados con la función oficial del app
        _refresh_pond_runtime_cache(POND_ID, db)

        db.commit()

        after = snapshot(db)
        show("DESPUÉS", after)
        print("\n✅ Reparación aplicada y commit OK.")

    except Exception:
        db.rollback()
        print("\n❌ Error: se hizo rollback, la BD quedó intacta.")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
