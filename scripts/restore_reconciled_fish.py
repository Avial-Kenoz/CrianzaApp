"""Restaura un pez que fue reconciliado por error (se encontro vivo).

Herramienta de SERVIDOR, de uso manual y puntual. Deliberadamente NO esta
expuesta en la app: revertir una reconciliacion es una correccion de datos que
debe quedar bajo control de quien administra la BD.

Dado el internal_id (PIT tag) de un pez en estado 'reconciled', revierte
EXACTAMENTE el efecto de su ultima reconciliacion:

  1) fish                 : state -> 'alive', devious_time -> NULL
  2) ponds_movements      : borra el movimiento de salida (reconciliation, destiny NULL)
  3) tag_detachment_events: si el pez era un re-tag cerrado por esa reconciliacion,
                            lo revierte a 'retagged' / sin resolver
  4) ponds (origen)       : repone la biomasa individual que la reconciliacion resto
                            y recomputa los contadores derivados (funcion oficial del app)

El estanque destino se deduce solo: es el source_pond_id del movimiento de salida.
Todo se hace en UNA transaccion, con respaldo previo de las filas afectadas a JSON.

Uso:
  python scripts/restore_reconciled_fish.py 8074318            # pide confirmacion
  python scripts/restore_reconciled_fish.py 8074318 --dry-run  # solo muestra el plan
  python scripts/restore_reconciled_fish.py 8074318 --yes      # sin confirmacion interactiva
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


def _norm_tag(value: str) -> str:
    return str(value).strip().upper()


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    return str(o)


def _row(db, sql, **params):
    return db.execute(text(sql), params).mappings().first()


def _rows(db, sql, **params):
    return [dict(r) for r in db.execute(text(sql), params).mappings().all()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Restaura un pez reconciliado por error.")
    ap.add_argument("internal_id", help="PIT tag (internal_id) del pez")
    ap.add_argument("--dry-run", action="store_true", help="Muestra el plan sin aplicar cambios")
    ap.add_argument("--yes", action="store_true", help="No pide confirmacion interactiva")
    args = ap.parse_args()

    tag = _norm_tag(args.internal_id)
    db = SessionLocal()
    try:
        # ── 1) Resolver el pez ──
        fish = _row(db, "SELECT * FROM fish WHERE internal_id = :t", t=tag)
        if fish is None:
            raise SystemExit(f"ABORT: no existe ningun pez con internal_id '{tag}'")
        fid = fish["id"]
        if fish["state"] != "reconciled":
            raise SystemExit(
                f"ABORT: el pez {tag} (id {fid}) esta en estado '{fish['state']}', no 'reconciled'. "
                "Nada que revertir."
            )

        # ── 2) Identificar el movimiento de salida por reconciliacion (ultimo del pez) ──
        movs = _rows(
            db,
            "SELECT * FROM ponds_movements WHERE fish_id = :f ORDER BY movement_time DESC, id DESC",
            f=fid,
        )
        if not movs:
            raise SystemExit(f"ABORT: el pez {tag} no tiene movimientos. No se puede deducir el estanque.")
        last = movs[0]
        if not (last["movement_reason"] == "reconciliation" and last["destiny_pond_id"] is None
                and last["source_pond_id"] is not None):
            raise SystemExit(
                "ABORT: el ultimo movimiento del pez no es una salida por reconciliacion "
                f"(reason={last['movement_reason']}, source={last['source_pond_id']}, "
                f"destiny={last['destiny_pond_id']}). Revisar manualmente."
            )
        recon_mov_id = last["id"]
        recon_time = last["movement_time"]
        restore_pond_id = last["source_pond_id"]

        # 2b) El movimiento previo debe haber dejado al pez en ese mismo estanque
        prev = movs[1] if len(movs) > 1 else None
        if prev is None or prev["destiny_pond_id"] != restore_pond_id:
            raise SystemExit(
                f"ABORT: el movimiento previo no deja al pez en el estanque {restore_pond_id} "
                f"(previo destiny={prev['destiny_pond_id'] if prev else None}). "
                "Borrar la salida no lo reubicaria correctamente. Revisar manualmente."
            )

        pond = _row(
            db,
            "SELECT id,name,state,biomass_current,tagged_count,n_fish_cached FROM ponds WHERE id = :i",
            i=restore_pond_id,
        )

        # ── 3) Evento(s) de re-tag cerrados por esta reconciliacion ──
        retag_events = _rows(
            db,
            "SELECT * FROM tag_detachment_events "
            "WHERE retag_fish_id = :f AND status = 'written_off' AND resolved_at = :t",
            f=fid,
            t=recon_time,
        )

        # ── 4) Biomasa a reponer (mismo estimado que la reconciliacion resto) ──
        peso_g = _get_fish_weight_estimate(fid, fish["lot_id"], db, pond_id=restore_pond_id)
        will_touch_biomass = bool(peso_g) and pond is not None and pond["biomass_current"] is not None
        delta_kg = Decimal(str(peso_g)) / Decimal("1000") if peso_g else Decimal("0")

        # ── Plan ──
        print(f"\nPez:       id={fid}  internal_id={tag}  lot_id={fish['lot_id']}  state={fish['state']}")
        print(f"Estanque:  {restore_pond_id} - {pond['name'] if pond else '?'} "
              f"(biomass_current={pond['biomass_current'] if pond else '?'}, "
              f"tagged_count={pond['tagged_count'] if pond else '?'})")
        print("\nPlan de reparacion:")
        print(f"  1) fish {fid}: state 'reconciled' -> 'alive', devious_time -> NULL")
        print(f"  2) borrar ponds_movements {recon_mov_id} (reconciliation {recon_time}, "
              f"source={restore_pond_id} -> NULL)")
        if retag_events:
            ids = ", ".join(str(e["id"]) for e in retag_events)
            print(f"  3) tag_detachment_events [{ids}]: 'written_off' -> 'retagged' / resolution NULL / resolved_at NULL")
        else:
            print("  3) (sin evento de re-tag asociado; el pez no era re-tag o no fue cerrado por esta reconciliacion)")
        if will_touch_biomass:
            new_b = Decimal(str(pond["biomass_current"])) + delta_kg
            print(f"  4) ponds {restore_pond_id}: biomass_current +{delta_kg} kg "
                  f"({pond['biomass_current']} -> {new_b}) + recomputo de contadores")
        else:
            print(f"  4) ponds {restore_pond_id}: solo recomputo de contadores "
                  f"(biomasa: nada que reponer; peso_g={peso_g}, biomass_current={pond['biomass_current'] if pond else None})")

        if args.dry_run:
            print("\n[DRY-RUN] No se aplican cambios.")
            return

        if not args.yes:
            resp = input("\nConfirmar y aplicar? [y/N] ").strip().lower()
            if resp not in ("y", "yes", "s", "si"):
                print("Cancelado. No se aplico ningun cambio.")
                return

        # ── Respaldo de las filas afectadas (solo al aplicar) ──
        backup = {
            "fish": fish,
            "ponds_movements_exit": last,
            "tag_detachment_events": retag_events,
            "ponds_origin": pond,
            "computed": {"restore_pond_id": restore_pond_id, "recon_time": recon_time,
                         "peso_g": peso_g, "delta_kg": delta_kg},
        }
        backups_dir = PROJECT_ROOT / "scripts" / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backups_dir / f"restore_fish_{tag}_{stamp}.json"
        backup_path.write_text(json.dumps(backup, default=_json_default, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nRespaldo de filas afectadas: {backup_path}")

        # ── Aplicar (una sola transaccion) ──
        now = datetime.utcnow()
        db.execute(text("UPDATE fish SET state='alive', devious_time=NULL, updated_at=:n WHERE id=:i"),
                   {"n": now, "i": fid})
        db.execute(text("DELETE FROM ponds_movements WHERE id=:i"), {"i": recon_mov_id})
        for ev in retag_events:
            db.execute(text(
                "UPDATE tag_detachment_events "
                "SET status='retagged', resolution=NULL, resolved_at=NULL WHERE id=:i"
            ), {"i": ev["id"]})
        db.flush()
        if will_touch_biomass:
            db.execute(text(
                "UPDATE ponds SET biomass_current = COALESCE(biomass_current,0) + :d, updated_at=:n WHERE id=:i"
            ), {"d": delta_kg, "n": now, "i": restore_pond_id})
        _refresh_pond_runtime_cache(restore_pond_id, db)
        db.commit()

        # ── Verificacion ──
        after_fish = _row(db, "SELECT state, devious_time FROM fish WHERE id=:i", i=fid)
        after_pond = _row(db, "SELECT tagged_count, n_fish_cached, biomass_current FROM ponds WHERE id=:i",
                          i=restore_pond_id)
        print("\nOK. Cambios aplicados (commit).")
        print(f"  fish {fid}: state={after_fish['state']} devious_time={after_fish['devious_time']}")
        print(f"  pond {restore_pond_id}: tagged_count={after_pond['tagged_count']} "
              f"n_fish_cached={after_pond['n_fish_cached']} biomass_current={after_pond['biomass_current']}")

    except Exception:
        db.rollback()
        print("\nError: rollback aplicado, la BD quedo intacta.")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
