"""Diagnostico READ-ONLY: divergencia de recuento entre /ui/ponds (cache) y /ui/ponds/{id} (vivo).

Para cada estanque compara:
  - cache:   n_fish_cached, tagged_count, unregistered_count (columnas persistidas)
  - vivo:    tagged (helper detalle), unregistered (helper), retagged sin resolver
  - list_shows   = n_fish_cached                                    (lo que muestra /ui/ponds)
  - detail_shows = live_tagged + live_unregistered - live_retagged  (net_fish_count del detalle)
  - cache_should = live_tagged + live_unregistered - live_retagged  (formula del cache, fresca)

No escribe nada. Uso:
  DATABASE_URL=postgresql://fastapp_user:fastapp_pass@localhost/fastapp_etapa1 \
    python scripts/diag_pond_count_divergence.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func

from app.db.session import SessionLocal, engine
from app.models.ponds import Pond
from app.models.tag_detachment_events import TagDetachmentEvent
from app.api.views import (
    _get_current_tagged_fish_in_pond,
    _get_unregistered_balances_by_lot,
)


def live_retagged(pond_id, db):
    return db.query(func.count(TagDetachmentEvent.id)).filter(
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status == "retagged",
        TagDetachmentEvent.resolved_at.is_(None),
    ).scalar() or 0


def main():
    print(f"[db] {engine.url}")
    db = SessionLocal()
    try:
        ponds = db.query(Pond).filter(Pond.state != "inactive").order_by(Pond.id).all()
        name_by_id = {p.id: p.name for p in ponds}

        rows = []
        for p in ponds:
            live_tagged = len(_get_current_tagged_fish_in_pond(p.id, db))
            live_unreg = int(sum(_get_unregistered_balances_by_lot(p.id, db).values()))
            live_retag = int(live_retagged(p.id, db))

            list_shows = int(p.n_fish_cached or 0)
            # El detalle ahora muestra net_fish_count = con + sin - retag (igual que el cache fresco).
            detail_shows = live_tagged + live_unreg - live_retag
            cache_should = live_tagged + live_unreg - live_retag

            rows.append({
                "id": p.id,
                "name": p.name,
                "parent": name_by_id.get(p.parent_pond_id) if p.parent_pond_id else "",
                "cache_tagged": int(p.tagged_count or 0),
                "cache_unreg": int(p.unregistered_count or 0),
                "live_tagged": live_tagged,
                "live_unreg": live_unreg,
                "live_retag": live_retag,
                "list_shows": list_shows,
                "detail_shows": detail_shows,
                "cache_should": cache_should,
                "stale": list_shows != cache_should,   # cache desalineado del valor fresco
                "visible_gap": list_shows != detail_shows,  # lo que el usuario ve distinto
            })

        # 1) Foco Central 1 / C1S
        focus = [r for r in rows if "central 1" in r["name"].lower()
                 or "central 1" in (r["parent"] or "").lower()]
        print("\n=== FOCO: Central 1 y sus subestanques ===")
        _print_table(focus)

        # 2) Todas las divergencias
        diverge = [r for r in rows if r["stale"] or r["visible_gap"]]
        print(f"\n=== TODAS las divergencias ({len(diverge)} de {len(rows)} estanques) ===")
        _print_table(diverge)

        n_stale = sum(1 for r in rows if r["stale"])
        n_visible = sum(1 for r in rows if r["visible_gap"])
        n_retag = sum(1 for r in rows if r["live_retag"])
        print(f"\nResumen: {n_stale} con cache stale | {n_visible} con gap visible "
              f"list vs detalle | {n_retag} con retag sin resolver")
    finally:
        db.close()


def _print_table(rows):
    if not rows:
        print("  (ninguno)")
        return
    hdr = ("id", "name", "parent", "list", "detail", "should",
           "l_tag", "l_unrg", "l_retg", "stale", "gap")
    print("  " + " | ".join(f"{h:>7}" if i >= 3 else f"{h:<16}"
                            for i, h in enumerate(hdr)))
    for r in rows:
        cells = [
            f"{r['id']:<16}"[:16] if False else f"{str(r['id']):<16}",
            f"{r['name']:<16}"[:16],
            f"{(r['parent'] or '-'):<16}"[:16],
            f"{r['list_shows']:>7}",
            f"{r['detail_shows']:>7}",
            f"{r['cache_should']:>7}",
            f"{r['live_tagged']:>7}",
            f"{r['live_unreg']:>7}",
            f"{r['live_retag']:>7}",
            f"{('YES' if r['stale'] else '-'):>7}",
            f"{('YES' if r['visible_gap'] else '-'):>7}",
        ]
        print("  " + " | ".join(cells))


if __name__ == "__main__":
    main()
