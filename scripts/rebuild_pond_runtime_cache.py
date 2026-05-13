"""Recalcula y persiste cache operativo por estanque.

Uso:
  source venv/bin/activate && python scripts/rebuild_pond_runtime_cache.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow direct execution: python scripts/rebuild_pond_runtime_cache.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.session import SessionLocal
from app.models.ponds import Pond
from app.api.views import _refresh_pond_runtime_cache


def main() -> None:
    db = SessionLocal()
    try:
        ponds = db.query(Pond).order_by(Pond.id).all()
        updated = 0
        for pond in ponds:
            _refresh_pond_runtime_cache(pond.id, db)
            updated += 1

        db.commit()
        print(f"updated_ponds={updated}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
