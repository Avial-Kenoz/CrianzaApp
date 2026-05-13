from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.views import (
    _adjust_biomass_on_movement,
    _get_fish_weight_estimate,
    _get_lot_weight_estimate,
    _get_unregistered_balances_by_lot,
)
from app.db.session import SessionLocal
from app.models.fish import Fish
from app.models.fish_samplings import FishSampling
from app.models.pond_lot_stats import PondLotStats
from app.models.ponds import Pond
from app.models.ponds_movements import PondMovement


def _pond_biomass(db, pond_id: int) -> float:
    row = db.query(Pond.biomass_current).filter(Pond.id == pond_id).first()
    if not row or row[0] is None:
        return 0.0
    return float(row[0])


def _pick_tagged_case(db):
    fish_rows = (
        db.query(Fish.id, Fish.lot_id)
        .join(FishSampling, FishSampling.fish_id == Fish.id)
        .group_by(Fish.id, Fish.lot_id)
        .all()
    )
    for fish_id, lot_id in fish_rows:
        src_row = (
            db.query(PondMovement.destiny_pond_id, PondMovement.source_pond_id)
            .filter(PondMovement.fish_id == fish_id)
            .order_by(PondMovement.movement_time.desc(), PondMovement.id.desc())
            .first()
        )
        if not src_row:
            continue

        src_id = src_row[0] or src_row[1]
        if not src_id:
            continue

        w_g = _get_fish_weight_estimate(int(fish_id), int(lot_id), db)
        src_biomass = _pond_biomass(db, int(src_id))
        if w_g is None or src_biomass <= (w_g / 1000.0):
            continue

        dst_row = db.query(Pond.id).filter(Pond.id != src_id).first()
        if not dst_row:
            continue

        return {
            "fish_id": int(fish_id),
            "lot_id": int(lot_id),
            "src_id": int(src_id),
            "dst_id": int(dst_row[0]),
        }
    return None


def _pick_untagged_case(db):
    ponds = [pid for (pid,) in db.query(Pond.id).all()]
    for src_id in ponds:
        balances = _get_unregistered_balances_by_lot(src_id, db)
        lot_qty = next(((lot, qty) for lot, qty in balances.items() if qty >= 20), None)
        if not lot_qty:
            continue

        lot_id, qty = lot_qty
        avg_g = _get_lot_weight_estimate(src_id, lot_id, db)
        if avg_g is None:
            continue
        qty_test = 10 if qty >= 10 else int(qty)
        delta_kg = (avg_g * qty_test) / 1000.0
        if _pond_biomass(db, src_id) <= delta_kg:
            continue

        dst_row = (
            db.query(PondLotStats.pond_id)
            .filter(PondLotStats.lot_id == lot_id, PondLotStats.pond_id != src_id)
            .first()
        )
        if not dst_row:
            dst_row = db.query(Pond.id).filter(Pond.id != src_id).first()
            if not dst_row:
                continue

        return {
            "src_id": int(src_id),
            "dst_id": int(dst_row[0]),
            "lot_id": int(lot_id),
            "qty": qty_test,
        }
    return None


def main() -> None:
    db = SessionLocal()
    try:
        print("VALIDATION START")

        tagged = _pick_tagged_case(db)
        if tagged:
            w_g = _get_fish_weight_estimate(tagged["fish_id"], tagged["lot_id"], db)
            if w_g is not None:
                sp = db.begin_nested()
                try:
                    delta_kg = w_g / 1000.0
                    src_before = _pond_biomass(db, tagged["src_id"])
                    dst_before = _pond_biomass(db, tagged["dst_id"])

                    mov = PondMovement(
                        fish_id=tagged["fish_id"],
                        lot_id=tagged["lot_id"],
                        source_pond_id=tagged["src_id"],
                        destiny_pond_id=tagged["dst_id"],
                        fish_quantity=1,
                        movement_reason="pond_movement",
                        movement_time=datetime.now(UTC),
                    )
                    db.add(mov)
                    db.flush()
                    _adjust_biomass_on_movement(mov, db)
                    db.flush()

                    src_after = _pond_biomass(db, tagged["src_id"])
                    dst_after = _pond_biomass(db, tagged["dst_id"])

                    print("CASE tagged_transfer")
                    print(f"expected_src_delta_kg={-delta_kg:.6f}")
                    print(f"actual_src_delta_kg={src_after - src_before:.6f}")
                    print(f"expected_dst_delta_kg={delta_kg:.6f}")
                    print(f"actual_dst_delta_kg={dst_after - dst_before:.6f}")
                finally:
                    sp.rollback()
            else:
                print("CASE tagged_transfer skipped=no_weight_estimate")
        else:
            print("CASE tagged_transfer skipped=no_data")

        untagged = _pick_untagged_case(db)
        if untagged:
            avg_g = _get_lot_weight_estimate(untagged["src_id"], untagged["lot_id"], db)
            if avg_g is not None and untagged["qty"] > 0:
                # Caso transferencia sin marcar
                sp_transfer = db.begin_nested()
                try:
                    delta_kg = (avg_g * untagged["qty"]) / 1000.0

                    src_before = _pond_biomass(db, untagged["src_id"])
                    dst_before = _pond_biomass(db, untagged["dst_id"])

                    dst_pre_balance = _get_unregistered_balances_by_lot(untagged["dst_id"], db).get(untagged["lot_id"], 0)
                    dst_pre_avg = _get_lot_weight_estimate(untagged["dst_id"], untagged["lot_id"], db) or avg_g
                    expected_dst_avg = (
                        ((dst_pre_balance * dst_pre_avg) + (untagged["qty"] * avg_g))
                        / (dst_pre_balance + untagged["qty"])
                        if (dst_pre_balance + untagged["qty"]) > 0
                        else avg_g
                    )

                    mov_transfer = PondMovement(
                        fish_id=None,
                        lot_id=untagged["lot_id"],
                        source_pond_id=untagged["src_id"],
                        destiny_pond_id=untagged["dst_id"],
                        fish_quantity=untagged["qty"],
                        movement_reason="pond_movement",
                        movement_time=datetime.now(UTC),
                    )
                    db.add(mov_transfer)
                    db.flush()
                    _adjust_biomass_on_movement(mov_transfer, db)
                    db.flush()

                    src_after_transfer = _pond_biomass(db, untagged["src_id"])
                    dst_after_transfer = _pond_biomass(db, untagged["dst_id"])
                    dst_stats = (
                        db.query(PondLotStats.avg_weight)
                        .filter(
                            PondLotStats.pond_id == untagged["dst_id"],
                            PondLotStats.lot_id == untagged["lot_id"],
                        )
                        .first()
                    )
                    dst_avg_after = float(dst_stats[0]) if dst_stats and dst_stats[0] is not None else None

                    print("CASE untagged_transfer")
                    print(f"expected_src_delta_kg={-delta_kg:.6f}")
                    print(f"actual_src_delta_kg={src_after_transfer - src_before:.6f}")
                    print(f"expected_dst_delta_kg={delta_kg:.6f}")
                    print(f"actual_dst_delta_kg={dst_after_transfer - dst_before:.6f}")
                    print(f"expected_dst_avg_g={expected_dst_avg:.6f}")
                    print(f"actual_dst_avg_g={(dst_avg_after if dst_avg_after is not None else -1):.6f}")
                finally:
                    sp_transfer.rollback()

                # Caso egreso sin marcar
                sp_exit = db.begin_nested()
                try:
                    delta_kg = (avg_g * untagged["qty"]) / 1000.0
                    src_before_exit = _pond_biomass(db, untagged["src_id"])
                    mov_exit = PondMovement(
                        fish_id=None,
                        lot_id=untagged["lot_id"],
                        source_pond_id=untagged["src_id"],
                        destiny_pond_id=None,
                        fish_quantity=untagged["qty"],
                        movement_reason="mortality",
                        movement_time=datetime.now(UTC),
                    )
                    db.add(mov_exit)
                    db.flush()
                    _adjust_biomass_on_movement(mov_exit, db)
                    db.flush()
                    src_after_exit = _pond_biomass(db, untagged["src_id"])
                    print("CASE untagged_exit")
                    print(f"expected_src_delta_kg={-delta_kg:.6f}")
                    print(f"actual_src_delta_kg={src_after_exit - src_before_exit:.6f}")
                finally:
                    sp_exit.rollback()
            else:
                print("CASE untagged_transfer skipped=no_weight_estimate")
                print("CASE untagged_exit skipped=no_weight_estimate")
        else:
            print("CASE untagged_transfer skipped=no_data")
            print("CASE untagged_exit skipped=no_data")

        negatives = db.query(func.count(Pond.id)).filter(func.coalesce(Pond.biomass_current, 0) < 0).scalar()
        print(f"negatives_during_test={int(negatives)}")
        db.rollback()
        print("VALIDATION END (rolled_back=true)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
