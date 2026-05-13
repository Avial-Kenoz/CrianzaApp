import sys
sys.path.insert(0, '.')
from app.db.database import SessionLocal
from app.models.pond import Pond
from app.models.cultivation_unit import CultivationUnit

db = SessionLocal()

# Reproduce el query pond_rows_raw
pond_rows_raw = (
    db.query(Pond, CultivationUnit)
    .outerjoin(CultivationUnit, CultivationUnit.id == Pond.cultivation_unit_id)
    .filter(Pond.parent_pond_id.is_(None))
    .all()
)

print(f"Total ponds_rows_raw: {len(pond_rows_raw)}")

# Buscar C4
for pond, cu in pond_rows_raw:
    if pond.id == 4:
        print(f"Found C4: id={pond.id}, name={pond.name}, active_lot_ids={pond.active_lot_ids}, lot_id={pond.lot_id}")
        break

# Pre-cargar hijos de C4
parent_ids = [pond.id for pond, _ in pond_rows_raw]
all_children = db.query(Pond).filter(Pond.parent_pond_id.in_(parent_ids)).all()
children_map = {}
for child in all_children:
    if child.parent_pond_id not in children_map:
        children_map[child.parent_pond_id] = []
    children_map[child.parent_pond_id].append(child)

print(f"Children of C4: {[c.name for c in children_map.get(4, [])]}")

# Ver lot_ids agregados desde C4 y sus hijos
lot_ids = set()
for pond, _ in pond_rows_raw:
    if pond.id == 4:
        print(f"C4 active_lot_ids: {pond.active_lot_ids}")
        if pond.active_lot_ids:
            for lid in pond.active_lot_ids:
                if lid:
                    lot_ids.add(int(lid))
                    print(f"  Added lot {lid} from C4")
        
        for child in children_map.get(4, []):
            print(f"  Child {child.name} active_lot_ids: {child.active_lot_ids}")
            if child.active_lot_ids:
                for lid in child.active_lot_ids:
                    if lid:
                        lot_ids.add(int(lid))
                        print(f"    Added lot {lid} from {child.name}")

print(f"Final lot_ids: {lot_ids}")
