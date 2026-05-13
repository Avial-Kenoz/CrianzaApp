from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func, or_
from typing import List, Optional
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from collections import defaultdict
from urllib.parse import quote_plus
import re
import json
from app.db.session import SessionLocal
from app.models.ponds import Pond
from app.models.pond_types import PondType
from app.models.fish import Fish
from app.models.fish_samplings import FishSampling
from app.models.depuration_periodic_samplings import DepurationPeriodicSampling
from app.models.lots import Lot
from app.models.ponds_movements import PondMovement
from app.models.cultivation_units import CultivationUnit
from app.models.sampling_sessions import SamplingSession
from app.models.sampling_records import SamplingRecord
from app.models.pond_lot_stats import PondLotStats
from app.models.tag_detachment_events import TagDetachmentEvent
from app.models.fish_drug_uses import FishDrugUse
from app.models.species import Species
from app.schemas.views import CultivationUnitWithPonds, PondSummary, LotSummary

router = APIRouter(prefix="/views", tags=["views"])
template_dir = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)))

STALE_WEIGHT_DAYS = 90
RECENT_LOT_WEIGHT_LOOKBACK_DAYS = 365
MIN_RECENT_LOT_SAMPLES_FOR_FLOOR = 10

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _latest_movement_id_per_fish_subq(db: Session):
    """Subquery que devuelve el id del movimiento más reciente por pez (desempate: max id)."""
    max_time_subq = (
        db.query(
            PondMovement.fish_id,
            func.max(PondMovement.movement_time).label("max_time"),
        )
        .filter(PondMovement.fish_id.isnot(None))
        .group_by(PondMovement.fish_id)
        .subquery()
    )
    latest_id_subq = (
        db.query(
            PondMovement.fish_id,
            func.max(PondMovement.id).label("max_id"),
        )
        .join(
            max_time_subq,
            (max_time_subq.c.fish_id == PondMovement.fish_id) &
            (max_time_subq.c.max_time == PondMovement.movement_time),
        )
        .group_by(PondMovement.fish_id)
        .subquery()
    )
    return latest_id_subq


def _get_pond_summary(pond: Pond, db: Session) -> PondSummary:
    """
    Para un estanque dado, calcula:
    - n_fish: peces cuyo último movimiento tiene destiny_pond_id = pond.id y estado alive/depuration
    - active_lots: lotes distintos de esos peces
    """
    latest_id_subq = _latest_movement_id_per_fish_subq(db)

    # Peces cuyo último movimiento los dejó en este estanque
    current_fish = (
        db.query(Fish)
        .join(PondMovement, PondMovement.fish_id == Fish.id)
        .join(latest_id_subq, latest_id_subq.c.max_id == PondMovement.id)
        .filter(
            PondMovement.destiny_pond_id == pond.id,
            Fish.state.in_(["alive", "depuration"])
        )
        .all()
    )

    unregistered_balances = _get_unregistered_balances_by_lot(pond.id, db)
    unregistered_count = sum(unregistered_balances.values())
    n_fish = len(current_fish) + unregistered_count

    # Lotes activos: distintos lot_id de los peces actuales
    lot_ids = list({f.lot_id for f in current_fish if f.lot_id} | set(unregistered_balances.keys()))
    active_lots = db.query(Lot).filter(Lot.id.in_(lot_ids)).all() if lot_ids else []

    return PondSummary(
        id=pond.id,
        name=pond.name,
        internal_id=pond.internal_id,
        code=pond.code,
        depuration=pond.depuration,
        state="depuration" if pond.depuration else pond.state,
        volume=pond.volume,
        n_fish=n_fish,
        biomass=float(pond.biomass_current) if pond.biomass_current else None,
        avg_weight=float(pond.avg_weight) if pond.avg_weight else None,
        active_lots=[LotSummary(id=l.id, name=l.name, internal_id=l.internal_id) for l in active_lots]
    )


def _get_current_tagged_fish_in_pond(pond_id: int, db: Session) -> List[Fish]:
    """Obtiene peces con PIT tag cuyo ultimo movimiento los deja en el estanque."""
    latest_id_subq = _latest_movement_id_per_fish_subq(db)

    return (
        db.query(Fish)
        .join(PondMovement, PondMovement.fish_id == Fish.id)
        .join(latest_id_subq, latest_id_subq.c.max_id == PondMovement.id)
        .filter(
            PondMovement.destiny_pond_id == pond_id,
            Fish.state.in_(["alive", "depuration"])
        )
        .order_by(Fish.internal_id)
        .all()
    )


def _get_pending_drug_log_fish_query(db: Session):
    """Peces con sufijo _R pero sin registros en fish_drug_uses."""
    return (
        db.query(Fish)
        .outerjoin(FishDrugUse, FishDrugUse.fish_id == Fish.id)
        .filter(
            Fish.internal_id.isnot(None),
            func.right(func.upper(func.trim(Fish.internal_id)), 2) == "_R",
        )
        .group_by(Fish.id)
        .having(func.count(FishDrugUse.id) == 0)
    )


def _safe_ui_next_path(next_path: Optional[str]) -> str:
    """Permite redirección solo a rutas UI internas conocidas."""
    if not next_path:
        return "/views/ui/ponds"
    path = str(next_path).strip()
    if not path.startswith("/views/ui/"):
        return "/views/ui/ponds"
    if "\n" in path or "\r" in path:
        return "/views/ui/ponds"
    return path


def _get_unregistered_fish_count(pond_id: int, db: Session) -> int:
    """Calcula saldo de peces sin PIT tag usando movimientos con fish_id NULL."""
    return sum(_get_unregistered_balances_by_lot(pond_id, db).values())


def _get_unregistered_balances_by_lot(pond_id: int, db: Session) -> dict[int, int]:
    """Saldo actual de peces sin PIT tag por lote dentro de un estanque."""
    incoming_rows = (
        db.query(PondMovement.lot_id, func.coalesce(func.sum(PondMovement.fish_quantity), 0))
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.destiny_pond_id == pond_id,
            PondMovement.lot_id.isnot(None),
        )
        .group_by(PondMovement.lot_id)
        .all()
    )
    outgoing_rows = (
        db.query(PondMovement.lot_id, func.coalesce(func.sum(PondMovement.fish_quantity), 0))
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.source_pond_id == pond_id,
            PondMovement.lot_id.isnot(None),
        )
        .group_by(PondMovement.lot_id)
        .all()
    )

    balances: dict[int, int] = {}
    for lot_id, qty in incoming_rows:
        balances[int(lot_id)] = int(qty or 0)
    for lot_id, qty in outgoing_rows:
        balances[int(lot_id)] = balances.get(int(lot_id), 0) - int(qty or 0)

    return {lot_id: qty for lot_id, qty in balances.items() if qty > 0}


def _as_int_list(raw_value) -> list[int]:
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        values = raw_value
    elif isinstance(raw_value, tuple):
        values = list(raw_value)
    else:
        return []

    out: list[int] = []
    for item in values:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _refresh_pond_runtime_cache(pond_id: Optional[int], db: Session) -> None:
    if not pond_id:
        return

    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        return

    tagged_fish = _get_current_tagged_fish_in_pond(pond_id, db)
    tagged_count = len(tagged_fish)
    tagged_lot_ids = {int(f.lot_id) for f in tagged_fish if f.lot_id is not None}

    unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)
    unregistered_count = sum(unregistered_balances.values())
    unregistered_lot_ids = {int(lot_id) for lot_id in unregistered_balances.keys()}

    active_lot_ids = sorted(tagged_lot_ids | unregistered_lot_ids)
    unregistered_lot_ids_sorted = sorted(unregistered_lot_ids)

    pond.tagged_count = tagged_count
    pond.unregistered_count = unregistered_count
    pond.n_fish_cached = tagged_count + unregistered_count
    pond.active_lots_count = len(active_lot_ids)
    pond.active_lot_ids = active_lot_ids
    pond.unregistered_lot_ids = unregistered_lot_ids_sorted
    pond.unregistered_lot_conflict = len(unregistered_lot_ids_sorted) > 1


def _refresh_pond_runtime_cache_many(pond_ids, db: Session) -> None:
    unique_ids = sorted({int(pid) for pid in pond_ids if pid})
    for pid in unique_ids:
        _refresh_pond_runtime_cache(pid, db)


def _build_cached_pond_rows(
    ponds: List[Pond],
    db: Session,
    pond_types_map: Optional[dict[int, str]] = None,
    include_unregistered_flags: bool = False,
) -> list[dict]:
    all_lot_ids: set[int] = set()
    for pond in ponds:
        all_lot_ids.update(_as_int_list(pond.active_lot_ids))

    lots_map = {
        lot.id: lot
        for lot in db.query(Lot).filter(Lot.id.in_(all_lot_ids)).all()
    } if all_lot_ids else {}

    rows: list[dict] = []
    for pond in ponds:
        active_lot_ids = _as_int_list(pond.active_lot_ids)
        unregistered_lot_ids = set(_as_int_list(pond.unregistered_lot_ids))

        active_lots = []
        for lot_id in active_lot_ids:
            lot = lots_map.get(lot_id)
            if not lot:
                continue

            lot_item = {
                "id": int(lot.id),
                "name": lot.name,
                "internal_id": lot.internal_id,
            }
            if include_unregistered_flags:
                lot_item["is_unregistered_lot"] = int(lot.id) in unregistered_lot_ids
            active_lots.append(lot_item)

        row = {
            "id": pond.id,
            "name": pond.name,
            "internal_id": pond.internal_id,
            "code": pond.code,
            "depuration": pond.depuration,
            "state": "depuration" if pond.depuration else pond.state,
            "volume": pond.volume,
            "n_fish": int(pond.n_fish_cached or 0),
            "biomass": float(pond.biomass_current) if pond.biomass_current else None,
            "density": round(float(pond.biomass_current) / pond.volume, 2) if (pond.biomass_current and pond.volume and pond.volume > 0) else None,
            "avg_weight": float(pond.avg_weight) if pond.avg_weight else None,
            "active_lots": active_lots,
            "unregistered_lot_conflict": bool(pond.unregistered_lot_conflict),
            "biomass_measured": float(pond.biomass_measured) if pond.biomass_measured else None,
        }
        if pond_types_map is not None:
            row["pond_type_name"] = pond_types_map.get(pond.pond_type_id, "—")

        rows.append(row)

    return rows


@router.get("/ponds", response_model=List[PondSummary])
def view_ponds_by_cultivation_unit(
    cultivation_unit_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """
    Lista de estanques filtrada por unidad de cultivo.
    Para cada estanque retorna: n° de peces activos, biomasa y lotes activos.
    """
    query = db.query(Pond)
    if cultivation_unit_id:
        query = query.filter(Pond.cultivation_unit_id == cultivation_unit_id)
    ponds = query.order_by(Pond.name).all()
    cached_rows = _build_cached_pond_rows(ponds, db)
    return [
        PondSummary(
            id=row["id"],
            name=row["name"],
            internal_id=row.get("internal_id"),
            code=row.get("code"),
            depuration=row.get("depuration"),
            state=row.get("state"),
            volume=row.get("volume"),
            n_fish=row["n_fish"],
            biomass=row.get("biomass"),
            avg_weight=row.get("avg_weight"),
            active_lots=[LotSummary(**lot) for lot in row.get("active_lots", [])],
        )
        for row in cached_rows
    ]


@router.get("/ponds/{pond_id}", response_model=PondSummary)
def view_pond_detail(pond_id: int, db: Session = Depends(get_db)):
    """
    Detalle de un estanque: n° de peces activos, biomasa y lotes activos.
    """
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")
    cached_row = _build_cached_pond_rows([pond], db)[0]
    return PondSummary(
        id=cached_row["id"],
        name=cached_row["name"],
        internal_id=cached_row.get("internal_id"),
        code=cached_row.get("code"),
        depuration=cached_row.get("depuration"),
        state=cached_row.get("state"),
        volume=cached_row.get("volume"),
        n_fish=cached_row["n_fish"],
        biomass=cached_row.get("biomass"),
        avg_weight=cached_row.get("avg_weight"),
        active_lots=[LotSummary(**lot) for lot in cached_row.get("active_lots", [])],
    )


@router.get("/cultivation-units", response_model=List[dict])
def list_cultivation_units_for_filter(db: Session = Depends(get_db)):
    """
    Lista de unidades de cultivo disponibles para el filtro de estanques.
    """
    units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
    return [{"id": u.id, "name": u.name} for u in units]


@router.get("/ui/ponds", response_class=HTMLResponse)
def ui_ponds(
    request: Request,
    cultivation_unit_id: Optional[int] = None,
    status: Optional[str] = None,
    msg: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Vista HTML de estanques."""
    # Unidades de cultivo para el selector
    cultivation_units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()

    # Estanques filtrados
    query = db.query(Pond)
    if cultivation_unit_id:
        query = query.filter(Pond.cultivation_unit_id == cultivation_unit_id)
    ponds_db = query.order_by(Pond.name).all()

    # Cargar tipos de estanque
    pond_types_list = db.query(PondType).order_by(PondType.name).all()
    pond_types_map = {pt.id: pt.name for pt in pond_types_list}

    # Resumen rápido desde cache persistido por estanque.
    summaries = _build_cached_pond_rows(
        ponds=ponds_db,
        db=db,
        pond_types_map=pond_types_map,
        include_unregistered_flags=True,
    )

    # K promedio por estanque desde pond_lot_stats
    pond_ids = [pond.id for pond in ponds_db]
    k_rows = (
        db.query(PondLotStats.pond_id, func.avg(PondLotStats.condition_k))
        .filter(PondLotStats.pond_id.in_(pond_ids), PondLotStats.condition_k.isnot(None))
        .group_by(PondLotStats.pond_id)
        .all()
    ) if pond_ids else []
    k_map = {row[0]: round(float(row[1]), 3) for row in k_rows}
    for s in summaries:
        s["avg_condition_k"] = k_map.get(s["id"])

    # ── Agrupar estanques por padre ──
    pond_parent_map = {pond.id: pond.parent_pond_id for pond in ponds_db}
    summaries_by_id = {s["id"]: s for s in summaries}

    # Children index: parent_id -> list of child summary dicts
    children_index: dict[int, list[dict]] = {}
    child_ids: set[int] = set()
    for pond in ponds_db:
        if pond.parent_pond_id and pond.parent_pond_id in summaries_by_id:
            children_index.setdefault(pond.parent_pond_id, []).append(summaries_by_id[pond.id])
            child_ids.add(pond.id)

    grouped: list[dict] = []
    for s in summaries:
        pond_id = s["id"]
        if pond_id in child_ids:
            continue  # será renderizado bajo su padre
        children = children_index.get(pond_id, [])
        if children:
            # Agregar estadísticas del padre + hijos para preservar la lógica histórica de totales
            grouped_members = [s] + children
            agg_n_fish = sum(m["n_fish"] for m in grouped_members)
            agg_biomass_list = [m["biomass"] for m in grouped_members if m.get("biomass") is not None]
            agg_biomass = sum(agg_biomass_list) if agg_biomass_list else None
            agg_volume = sum(m.get("volume") or 0 for m in grouped_members)
            agg_density = round(agg_biomass / agg_volume, 2) if (agg_biomass and agg_volume) else None
            w_sum = sum(m["n_fish"] * m["avg_weight"] for m in grouped_members if m.get("avg_weight") is not None)
            w_cnt = sum(m["n_fish"] for m in grouped_members if m.get("avg_weight") is not None)
            agg_avg_weight = round(w_sum / w_cnt, 1) if w_cnt else None
            k_wsum = sum(m["n_fish"] * m["avg_condition_k"] for m in grouped_members if m.get("avg_condition_k") is not None)
            k_cnt = sum(m["n_fish"] for m in grouped_members if m.get("avg_condition_k") is not None)
            agg_k = round(k_wsum / k_cnt, 3) if k_cnt else None
            all_lots: dict[int, dict] = {}
            for member in grouped_members:
                for lot in member["active_lots"]:
                    all_lots[lot["id"]] = lot
            parent_row = dict(s)
            parent_row["n_fish"] = agg_n_fish
            parent_row["biomass"] = agg_biomass
            parent_row["density"] = agg_density
            parent_row["avg_weight"] = agg_avg_weight
            parent_row["avg_condition_k"] = agg_k
            parent_row["active_lots"] = list(all_lots.values())
            parent_row["is_parent_group"] = True
            parent_row["children"] = children
        else:
            parent_row = s
            parent_row["is_parent_group"] = False
            parent_row["children"] = []
        grouped.append(parent_row)

    # Tarjetas de resumen
    total_fish = sum(s["n_fish"] for s in summaries)
    total_biomass = sum(s["biomass"] for s in summaries if s["biomass"])
    total_volume = sum(s["volume"] for s in summaries if s.get("volume") and s["volume"] > 0 and s["biomass"])
    global_density = round(total_biomass / total_volume, 2) if total_volume else None
    k_values = [s["avg_condition_k"] for s in summaries if s.get("avg_condition_k")]
    avg_k = round(sum(k_values) / len(k_values), 3) if k_values else None
    pending_drug_logs_count = _get_pending_drug_log_fish_query(db).count()

    # Lista flat de estanques sin padre para el selector del formulario de creación
    all_parent_ponds = [
        {"id": p.id, "name": p.name}
        for p in ponds_db
        if p.parent_pond_id is None
    ]

    context = {
        "request": request,
        "ponds": grouped,
        "cultivation_units": [{"id": u.id, "name": u.name} for u in cultivation_units],
        "pond_types": [{"id": pt.id, "name": pt.name} for pt in pond_types_list],
        "all_parent_ponds": all_parent_ponds,
        "selected_cu": cultivation_unit_id,
        "status": status,
        "msg": msg,
        "stat_fish": total_fish,
        "stat_biomass": round(total_biomass, 1) if total_biomass else None,
        "stat_density": global_density,
        "stat_k": avg_k,
        "pending_drug_logs_count": pending_drug_logs_count,
    }
    template = jinja_env.get_template("ponds.html")
    html = template.render(context)
    return HTMLResponse(content=html)


@router.get("/ui/fish/pending-drug-logs", response_class=HTMLResponse)
def ui_pending_drug_logs(
    request: Request,
    status: Optional[str] = None,
    msg: Optional[str] = None,
    db: Session = Depends(get_db),
):
    pending_fish = _get_pending_drug_log_fish_query(db).order_by(Fish.updated_at.desc(), Fish.id.desc()).all()
    pond_name_map = {p.id: p.name for p in db.query(Pond).all()}

    rows = []
    for fish in pending_fish:
        lot = db.query(Lot).filter(Lot.id == fish.lot_id).first() if fish.lot_id else None
        last_mv = (
            db.query(PondMovement)
            .filter(PondMovement.fish_id == fish.id)
            .order_by(PondMovement.movement_time.desc(), PondMovement.id.desc())
            .first()
        )
        current_pond = pond_name_map.get(last_mv.destiny_pond_id) if last_mv and last_mv.destiny_pond_id else None
        rows.append({
            "fish_id": fish.id,
            "internal_id": _normalize_pit_tag(fish.internal_id),
            "state": fish.state or "N/D",
            "lot": lot.name if lot and lot.name else (lot.internal_id if lot else "N/D"),
            "current_pond": current_pond or "N/D",
        })

    template = jinja_env.get_template("fish_pending_drug_logs.html")
    html = template.render({
        "request": request,
        "status": status,
        "msg": msg,
        "rows": rows,
        "pending_count": len(rows),
    })
    return HTMLResponse(content=html)


@router.get("/ui/ponds/new", response_class=HTMLResponse)
def ui_pond_new(
    request: Request,
    parent_id: Optional[int] = None,
    status: Optional[str] = None,
    msg: Optional[str] = None,
    db: Session = Depends(get_db),
):
    cultivation_units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
    pond_types = db.query(PondType).order_by(PondType.name).all()
    all_ponds = db.query(Pond).filter(Pond.parent_pond_id.is_(None)).order_by(Pond.name).all()

    parent_name = None
    pre_cultivation_unit_id = None
    if parent_id:
        parent_obj = db.query(Pond).filter(Pond.id == parent_id).first()
        if parent_obj:
            parent_name = parent_obj.name
            pre_cultivation_unit_id = parent_obj.cultivation_unit_id

    template = jinja_env.get_template("pond_form.html")
    html = template.render({
        "request": request,
        "is_new": True,
        "pond": None,
        "all_ponds": [{"id": p.id, "name": p.name} for p in all_ponds],
        "cultivation_units": cultivation_units,
        "pond_types": pond_types,
        "pre_parent_id": parent_id,
        "parent_name": parent_name,
        "pre_cultivation_unit_id": pre_cultivation_unit_id,
        "status": status,
        "msg": msg,
    })
    return HTMLResponse(content=html)


@router.post("/ui/ponds/new")
def ui_pond_new_save(
    name: Optional[str] = Form(None),
    internal_id: Optional[str] = Form(None),
    code: Optional[str] = Form(None),
    volume: Optional[str] = Form(None),
    cultivation_unit_id: Optional[str] = Form(None),
    pond_type_id: Optional[str] = Form(None),
    depuration: Optional[str] = Form(None),
    state: Optional[str] = Form(None),
    parent_pond_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    def go_error(message: str):
        return RedirectResponse(
            url=f"/views/ui/ponds/new?status=error&msg={quote_plus(message)}",
            status_code=303,
        )

    name_value = (name or "").strip()
    if not name_value:
        return go_error("El nombre del estanque es obligatorio.")

    dep_raw = (depuration or "").strip().lower()
    depuration_value = dep_raw in {"1", "true", "t", "yes", "y", "on"}

    allowed_states = {"success", "active", "inactive", "depuration"}
    target_state = (state or "").strip().lower() or "success"
    if target_state not in allowed_states:
        return go_error("Estado no válido.")
    if depuration_value:
        target_state = "depuration"
    elif target_state == "depuration":
        return go_error("Si activa depuración, el estado no puede quedar en depuration sin activar el modo depuración.")

    internal_id_value = (internal_id or "").strip() or None
    code_value = (code or "").strip() or None

    raw_volume = (volume or "").strip()
    try:
        volume_value = int(raw_volume) if raw_volume else 0
    except ValueError:
        return go_error("El volumen debe ser un número entero.")
    if volume_value < 0:
        return go_error("El volumen no puede ser negativo.")

    cultivation_unit_value = None
    if cultivation_unit_id and cultivation_unit_id.strip():
        if not cultivation_unit_id.strip().isdigit():
            return go_error("Unidad de cultivo no válida.")
        cultivation_unit_value = int(cultivation_unit_id.strip())
        if not db.query(CultivationUnit.id).filter(CultivationUnit.id == cultivation_unit_value).first():
            return go_error("La unidad de cultivo seleccionada no existe.")

    pond_type_value = None
    if pond_type_id and pond_type_id.strip():
        if not pond_type_id.strip().isdigit():
            return go_error("Tipo de estanque no válido.")
        pond_type_value = int(pond_type_id.strip())
        if not db.query(PondType.id).filter(PondType.id == pond_type_value).first():
            return go_error("El tipo de estanque seleccionado no existe.")

    parent_pond_value = None
    raw_parent = (parent_pond_id or "").strip()
    if raw_parent and raw_parent != "0":
        if not raw_parent.isdigit():
            return go_error("Estanque padre no válido.")
        parent_pond_value = int(raw_parent)
        if not db.query(Pond.id).filter(Pond.id == parent_pond_value).first():
            return go_error("El estanque padre seleccionado no existe.")

    try:
        new_pond = Pond(
            name=name_value,
            internal_id=internal_id_value,
            code=code_value,
            volume=volume_value,
            cultivation_unit_id=cultivation_unit_value,
            pond_type_id=pond_type_value,
            depuration=depuration_value,
            state=target_state,
            parent_pond_id=parent_pond_value,
            n_fish_cached=0,
            tagged_count=0,
            unregistered_count=0,
            active_lots_count=0,
            active_lot_ids=[],
            unregistered_lot_ids=[],
            unregistered_lot_conflict=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(new_pond)
        db.commit()
        db.refresh(new_pond)
        return RedirectResponse(
            url=f"/views/ui/ponds/{new_pond.id}/edit?status=ok&msg={quote_plus('Estanque creado correctamente.')}",
            status_code=303,
        )
    except Exception:
        db.rollback()
        return go_error("No se pudo crear el estanque.")


@router.get("/ui/ponds/{pond_id}/edit", response_class=HTMLResponse)
def ui_pond_edit(
    pond_id: int,
    request: Request,
    status: Optional[str] = None,
    msg: Optional[str] = None,
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    cultivation_units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
    pond_types = db.query(PondType).order_by(PondType.name).all()
    # Candidatos a padre: todos los estanques excepto el actual y los que ya tienen este como padre
    all_ponds = (
        db.query(Pond)
        .filter(Pond.id != pond_id, Pond.parent_pond_id.is_(None))
        .order_by(Pond.name)
        .all()
    )

    template = jinja_env.get_template("pond_form.html")
    html = template.render({
        "request": request,
        "pond": pond,
        "cultivation_units": cultivation_units,
        "pond_types": pond_types,
        "all_ponds": [{"id": p.id, "name": p.name} for p in all_ponds],
        "status": status,
        "msg": msg,
    })
    return HTMLResponse(content=html)


@router.post("/ui/ponds/{pond_id}/edit")
def ui_pond_edit_save(
    pond_id: int,
    name: Optional[str] = Form(None),
    internal_id: Optional[str] = Form(None),
    code: Optional[str] = Form(None),
    volume: Optional[str] = Form(None),
    cultivation_unit_id: Optional[str] = Form(None),
    pond_type_id: Optional[str] = Form(None),
    depuration: Optional[str] = Form(None),
    state: Optional[str] = Form(None),
    parent_pond_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    def go(status_value: str, message: str):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}/edit?status={quote_plus(status_value)}&msg={quote_plus(message)}",
            status_code=303,
        )

    dep_raw = (depuration or "").strip().lower()
    depuration_value = dep_raw in {"1", "true", "t", "yes", "y", "on"}

    allowed_states = {"success", "active", "inactive", "depuration"}
    target_state = (state or "").strip().lower() or pond.state or "success"
    if target_state not in allowed_states:
        return go("error", "Estado no válido.")

    if depuration_value:
        target_state = "depuration"
    elif target_state == "depuration":
        return go("error", "Si desactiva depuración, el estado no puede quedar en depuration.")

    name_value = (name or "").strip()
    if not name_value:
        return go("error", "El nombre del estanque es obligatorio.")

    internal_id_value = (internal_id or "").strip() or None
    code_value = (code or "").strip() or None

    raw_volume = (volume or "").strip()
    try:
        volume_value = int(raw_volume) if raw_volume else 0
    except ValueError:
        return go("error", "El volumen debe ser un número entero.")
    if volume_value < 0:
        return go("error", "El volumen no puede ser negativo.")

    cultivation_unit_value = None
    if cultivation_unit_id and cultivation_unit_id.strip():
        if not cultivation_unit_id.strip().isdigit():
            return go("error", "Unidad de cultivo no válida.")
        cultivation_unit_value = int(cultivation_unit_id.strip())
        exists_cu = db.query(CultivationUnit.id).filter(CultivationUnit.id == cultivation_unit_value).first()
        if not exists_cu:
            return go("error", "La unidad de cultivo seleccionada no existe.")

    pond_type_value = None
    if pond_type_id and pond_type_id.strip():
        if not pond_type_id.strip().isdigit():
            return go("error", "Tipo de estanque no válido.")
        pond_type_value = int(pond_type_id.strip())
        exists_pt = db.query(PondType.id).filter(PondType.id == pond_type_value).first()
        if not exists_pt:
            return go("error", "El tipo de estanque seleccionado no existe.")

    # Validar estanque padre
    parent_pond_value = None
    raw_parent = (parent_pond_id or "").strip()
    if raw_parent and raw_parent != "0":
        if not raw_parent.isdigit():
            return go("error", "Estanque padre no válido.")
        parent_pond_value = int(raw_parent)
        if parent_pond_value == pond_id:
            return go("error", "Un estanque no puede ser su propio padre.")
        exists_parent = db.query(Pond.id).filter(Pond.id == parent_pond_value).first()
        if not exists_parent:
            return go("error", "El estanque padre seleccionado no existe.")
        # Evitar ciclos: el padre no puede tener ya a este estanque como padre
        parent_pond_obj = db.query(Pond).filter(Pond.id == parent_pond_value).first()
        if parent_pond_obj and parent_pond_obj.parent_pond_id == pond_id:
            return go("error", "Asignación circular: el estanque padre ya apunta a este estanque.")

    try:
        pond.name = name_value
        pond.internal_id = internal_id_value
        pond.code = code_value
        pond.volume = volume_value
        pond.cultivation_unit_id = cultivation_unit_value
        pond.pond_type_id = pond_type_value
        pond.depuration = depuration_value
        pond.state = target_state
        pond.parent_pond_id = parent_pond_value
        pond.updated_at = datetime.utcnow()
        db.commit()
        return go("ok", "Condiciones del estanque actualizadas correctamente.")
    except Exception:
        db.rollback()
        return go("error", "No se pudieron guardar los cambios del estanque.")


@router.get("/ui/ponds/{pond_id}", response_class=HTMLResponse)
def ui_pond_detail(
    pond_id: int,
    request: Request,
    status: Optional[str] = None,
    msg: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Vista HTML de detalle por laguna."""
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    current_fish = _get_current_tagged_fish_in_pond(pond_id, db)
    unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)
    unregistered_count = sum(unregistered_balances.values())

    lot_ids = list({f.lot_id for f in current_fish if f.lot_id})
    lots_map = {
        l.id: l for l in db.query(Lot).filter(Lot.id.in_(lot_ids)).all()
    } if lot_ids else {}

    latest_sampling_subq = (
        db.query(
            FishSampling.fish_id,
            func.max(
                func.coalesce(FishSampling.registry_time, FishSampling.created_at)
            ).label("max_sample_time"),
        )
        .group_by(FishSampling.fish_id)
        .subquery()
    )

    latest_samples = (
        db.query(FishSampling)
        .join(
            latest_sampling_subq,
            (latest_sampling_subq.c.fish_id == FishSampling.fish_id) &
            (
                latest_sampling_subq.c.max_sample_time ==
                func.coalesce(FishSampling.registry_time, FishSampling.created_at)
            )
        )
        .all()
    )
    samples_map = {s.fish_id: s for s in latest_samples}

    fish_ids = [f.id for f in current_fish]

    depuration_start_map = {}
    if fish_ids:
        movements = (
            db.query(PondMovement)
            .filter(PondMovement.fish_id.in_(fish_ids))
            .order_by(PondMovement.fish_id.asc(), PondMovement.movement_time.asc(), PondMovement.id.asc())
            .all()
        )

        pond_ids = {
            m.source_pond_id for m in movements if m.source_pond_id
        } | {
            m.destiny_pond_id for m in movements if m.destiny_pond_id
        }
        pond_depuration_map = {
            p.id: bool(p.depuration)
            for p in db.query(Pond).filter(Pond.id.in_(pond_ids)).all()
        } if pond_ids else {}

        cycle_start_by_fish = {}
        for movement in movements:
            fish_id = movement.fish_id
            if fish_id is None:
                continue

            src_is_depuration = bool(
                movement.source_pond_id and pond_depuration_map.get(movement.source_pond_id)
            )
            dst_is_depuration = bool(
                movement.destiny_pond_id and pond_depuration_map.get(movement.destiny_pond_id)
            )

            # La depuración se inicia solo en transición no-depuración -> depuración.
            if (not src_is_depuration) and dst_is_depuration:
                cycle_start_by_fish[fish_id] = movement.movement_time
            # La depuración termina al salir a estanque no-depuración.
            elif src_is_depuration and (not dst_is_depuration):
                cycle_start_by_fish[fish_id] = None

        depuration_start_map = cycle_start_by_fish

    fish_rows = []
    for fish in current_fish:
        lot = lots_map.get(fish.lot_id)
        sample = samples_map.get(fish.id)
        sex_norm = (fish.sex or "").strip().lower()
        is_female = sex_norm in ["f", "female", "hembra"]
        sex_label = "Hembra" if is_female else ("Macho" if sex_norm in ["m", "male", "macho"] else "N/D")

        depuration_days = None
        if fish.state == "depuration":
            dep_start = None
            dep_cycle_start = depuration_start_map.get(fish.id)
            if dep_cycle_start:
                dep_start = dep_cycle_start
            elif fish.depuration_start_time:
                dep_start = fish.depuration_start_time

            if dep_start:
                depuration_days = (datetime.utcnow().date() - dep_start.date()).days

        fish_rows.append({
            "fish_id": fish.id,
            "internal_id": fish.internal_id or "N/D",
            "lot": (lot.internal_id or lot.name) if lot else "N/D",
            "sex": sex_label,
            "sex_value": "f" if is_female else ("m" if sex_norm in ["m", "male", "macho"] else ""),
            "last_weight": float(sample.weight) if sample and sample.weight is not None else None,
            "last_caviar_diameter": (
                float(sample.diameter)
                if is_female and sample and sample.diameter is not None
                else None
            ),
            "development_state": (
                str(sample.development_state).strip().upper()
                if sample and sample.development_state
                else ""
            ),
            "depuration_days": depuration_days,
        })

    show_depuration_column = pond.depuration or any(row["depuration_days"] is not None for row in fish_rows)

    all_ponds = db.query(Pond).order_by(Pond.name).all()

    # Eventos de pérdida de tag pendientes (sin re-tagear) en este estanque
    pending_detachment_events = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status == "unidentified",
    ).order_by(TagDetachmentEvent.event_date.asc()).all()

    unregistered_lot_ids = list(unregistered_balances.keys())
    unregistered_lots = (
        db.query(Lot).filter(Lot.id.in_(unregistered_lot_ids)).all()
        if unregistered_lot_ids else []
    )
    unregistered_lots_map = {lot.id: lot for lot in unregistered_lots}
    unregistered_lot_options = []
    for lot_id, qty in sorted(unregistered_balances.items()):
        lot = unregistered_lots_map.get(lot_id)
        lot_label = lot.internal_id if lot and lot.internal_id else (lot.name if lot else str(lot_id))
        unregistered_lot_options.append({
            "id": lot_id,
            "label": lot_label,
            "qty": qty,
        })

    females_state_4_count = sum(
        1
        for row in fish_rows
        if row.get("sex_value") == "f" and row.get("development_state") == "4"
    )
    females_state_4_biomass_kg = sum(
        float(row.get("last_weight") or 0) / 1000.0
        for row in fish_rows
        if row.get("sex_value") == "f" and row.get("development_state") == "4"
    )
    caviar_estimated_kg = females_state_4_biomass_kg * 0.125

    template = jinja_env.get_template("pond_detail.html")
    html = template.render({
        "request": request,
        "pond": {
            "id": pond.id,
            "name": pond.name,
            "code": pond.code,
            "internal_id": pond.internal_id,
            "depuration": pond.depuration,
        },
        "fish_rows": fish_rows,
        "show_depuration_column": show_depuration_column,
        "all_ponds": [{"id": p.id, "name": p.name} for p in all_ponds if p.id != pond.id],
        "tagged_count": len(fish_rows),
        "unregistered_count": unregistered_count,
        "female_state_4_count": females_state_4_count,
        "female_state_4_biomass_kg": round(females_state_4_biomass_kg, 1),
        "caviar_estimated_kg": round(caviar_estimated_kg, 1),
        "can_register_from_untagged": unregistered_count > 0,
        "unregistered_lot_options": unregistered_lot_options,
        "pending_detachment_events": [
            {"id": e.id, "event_date": e.event_date, "notes": e.notes}
            for e in pending_detachment_events
        ],
        "status": status,
        "msg": msg,
    })
    return HTMLResponse(content=html)


def _normalize_sex_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("f", "female", "hembra"):
        return "f"
    if v in ("m", "male", "macho"):
        return "m"
    if v in ("", "nd", "n/d", "none", "null"):
        return None
    return None


def _parse_decimal_field(value: Optional[str], field_name: str) -> Optional[Decimal]:
    if value is None:
        return None
    raw = value.strip().replace(",", ".")
    if raw == "":
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field_name} no es numérico.")


def _normalize_pit_tag(value: Optional[str]) -> str:
    """Normaliza PIT tag como texto (nunca como número)."""
    if value is None:
        return ""
    return str(value).strip().upper()


def _check_pit_tag_reuse(pit_tag: str, db: Session) -> dict:
    """
    Checks if a PIT tag can be registered.
    Returns:
      {"status": "ok"}                      - tag is free
      {"status": "blocked", "message": ...} - alive/depuration fish has this tag
      {"status": "reuse",   "message": ...} - tag exists only on dead fish, reuse allowed with confirmation
    """
    existing = db.query(Fish).filter(Fish.internal_id == pit_tag).all()
    if not existing:
        return {"status": "ok"}
    alive = [f for f in existing if f.state in ("alive", "depuration", "faena")]
    if alive:
        return {
            "status": "blocked",
            "message": f"El PIT tag {pit_tag} está activo en un pez vivo (id={alive[0].id}). No se puede reutilizar.",
        }
    indexed = db.query(Fish).filter(Fish.internal_id.op("~")(f"^{re.escape(pit_tag)}_[0-9]+$")).all()
    pattern = re.compile(rf"^{re.escape(pit_tag)}_(\d+)$")
    used = {int(m.group(1)) for f in indexed if (m := pattern.match(f.internal_id))}
    next_idx = 1
    while next_idx in used:
        next_idx += 1
    return {
        "status": "reuse",
        "message": (
            f"El PIT tag {pit_tag} fue usado por un pez fallecido. "
            f"Al confirmar, el pez fallecido pasará a llamarse {pit_tag}_{next_idx} "
            f"y el nuevo pez recibirá el tag {pit_tag}."
        ),
        "next_idx": next_idx,
    }


def _archive_dead_fish_tag(pit_tag: str, next_idx: int, db: Session) -> None:
    """Renames the dead base-tag fish to pit_tag_N to free up the tag."""
    dead = db.query(Fish).filter(
        Fish.internal_id == pit_tag,
        Fish.state.notin_(["alive", "depuration", "faena"])
    ).first()
    if dead:
        dead.internal_id = f"{pit_tag}_{next_idx}"
        dead.updated_at = datetime.utcnow()


def _normalize_development_state(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().upper()
    return v if v else None


def _is_valid_development_state_for_sex(dev_state: Optional[str], sex_value: Optional[str]) -> bool:
    if not dev_state:
        return True
    female_states = {"0", "1", "2", "3", "4", "R"}
    male_states = {"0", "L"}
    if sex_value == "f":
        return dev_state in female_states
    if sex_value == "m":
        return dev_state in male_states
    return False


def _resolve_destination_pond(move_to: Optional[str], db: Session) -> Optional[Pond]:
    if not move_to:
        return None
    text = move_to.strip()
    if not text:
        return None

    # Soporta: "17", "17 - Batea 1" o nombre exacto de estanque.
    if text.isdigit():
        return db.query(Pond).filter(Pond.id == int(text)).first()

    if "-" in text:
        maybe_id = text.split("-", 1)[0].strip()
        if maybe_id.isdigit():
            pond = db.query(Pond).filter(Pond.id == int(maybe_id)).first()
            if pond:
                return pond

    return db.query(Pond).filter(Pond.name == text).first()


@router.post("/ui/ponds/{pond_id}/fish/{fish_id}/save")
def ui_pond_fish_save(
    pond_id: int,
    fish_id: int,
    sex: Optional[str] = Form(None),
    weight: Optional[str] = Form(None),
    diameter: Optional[str] = Form(None),
    development_state: Optional[str] = Form(None),
    move_to: Optional[str] = Form(None),
    action: str = Form("save"),
    db: Session = Depends(get_db),
):
    redirect_base = f"/views/ui/ponds/{pond_id}"

    def go(status_value: str, message: str):
        return RedirectResponse(
            url=f"{redirect_base}?status={quote_plus(status_value)}&msg={quote_plus(message)}",
            status_code=303,
        )

    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        return go("error", "Estanque no encontrado.")

    fish = db.query(Fish).filter(Fish.id == fish_id).first()
    if not fish:
        return go("error", "Pez no encontrado.")
    if fish.state not in ("alive", "depuration"):
        return go("error", f"El pez {fish.internal_id or fish.id} no está activo.")

    # Validar que el pez siga actualmente en este estanque.
    last_movement = (
        db.query(PondMovement)
        .filter(PondMovement.fish_id == fish.id)
        .order_by(PondMovement.movement_time.desc(), PondMovement.id.desc())
        .first()
    )
    if not last_movement or last_movement.destiny_pond_id != pond_id:
        return go("error", "El pez ya no está en este estanque. Recarga la vista.")

    try:
        new_weight = _parse_decimal_field(weight, "Peso")
        new_diameter = _parse_decimal_field(diameter, "Diámetro")
    except ValueError as exc:
        return go("error", str(exc))

    if new_weight is not None and new_weight <= 0:
        return go("error", "El peso debe ser mayor a 0.")
    if new_diameter is not None and new_diameter <= 0:
        return go("error", "El diámetro debe ser mayor a 0.")

    target_sex = _normalize_sex_value(sex)
    current_sex = _normalize_sex_value(fish.sex)
    sex_changed = current_sex != target_sex

    latest_sample = (
        db.query(FishSampling)
        .filter(FishSampling.fish_id == fish.id)
        .order_by(
            func.coalesce(FishSampling.registry_time, FishSampling.created_at).desc(),
            FishSampling.id.desc(),
        )
        .first()
    )
    current_weight = Decimal(str(latest_sample.weight)) if latest_sample and latest_sample.weight is not None else None
    current_diameter = Decimal(str(latest_sample.diameter)) if latest_sample and latest_sample.diameter is not None else None
    current_development_state = _normalize_development_state(latest_sample.development_state if latest_sample else None)
    target_development_state = _normalize_development_state(development_state)
    weight_changed = current_weight != new_weight
    diameter_changed = current_diameter != new_diameter
    development_state_changed = current_development_state != target_development_state

    if development_state_changed and not target_sex:
        return go("error", "Para actualizar estado de desarrollo debe indicar sexo (hembra o macho).")

    if not _is_valid_development_state_for_sex(target_development_state, target_sex):
        if target_sex == "f":
            return go("error", "Estado de desarrollo inválido para hembra. Use: 0, 1, 2, 3, 4 o R.")
        if target_sex == "m":
            return go("error", "Estado de desarrollo inválido para macho. Use: 0 o L.")
        return go("error", "Estado de desarrollo inválido.")

    destination = _resolve_destination_pond(move_to, db)
    movement_requested = bool(move_to and move_to.strip())
    if movement_requested and not destination:
        return go("error", "Estanque destino no válido.")
    if destination and destination.id == pond_id:
        return go("error", "El estanque destino debe ser distinto al actual.")

    # ── Acción FAENA ────────────────────────────────────────────────────────
    if action == "faena":
        if not pond.depuration:
            return go("error", "Faena solo se puede registrar desde estanques de depuración.")
        if not target_sex:
            return go("error", f"Pez {fish.internal_id}: debe indicar sexo antes de retirar a faena.")
        if not new_weight or new_weight <= 0:
            return go("error", f"Pez {fish.internal_id}: debe ingresar el peso antes de retirar a faena.")
        if target_sex == "f" and (not new_diameter or new_diameter <= 0):
            return go("error", f"Pez {fish.internal_id}: debe ingresar el diámetro de caviar (hembra) antes de retirar a faena.")

        now = datetime.utcnow()
        try:
            if sex_changed:
                fish.sex = target_sex

            # Guardar muestreo final con los datos actualizados
            sample = FishSampling(
                fish_id=fish.id,
                weight=new_weight,
                diameter=new_diameter if target_sex == "f" else None,
                development_state=target_development_state,
                registry_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(sample)

            # Movimiento de salida sin destino
            faena_movement = PondMovement(
                fish_id=fish.id,
                lot_id=fish.lot_id,
                source_pond_id=pond_id,
                destiny_pond_id=None,
                fish_quantity=1,
                movement_reason="faena",
                movement_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(faena_movement)

            _adjust_biomass_on_movement(faena_movement, db)
            _refresh_pond_runtime_cache_many([pond_id], db)

            fish.state = "faena"
            fish.updated_at = now
            db.commit()
            return go("ok", f"Pez {fish.internal_id} enviado a faena. Pendiente confirmación.")
        except Exception:
            db.rollback()
            return go("error", "No se pudo registrar la faena.")

    # ── Acción MORTALIDAD ───────────────────────────────────────────────────
    if action == "mortality":
        now = datetime.utcnow()
        try:
            mortality_movement = PondMovement(
                fish_id=fish.id,
                lot_id=fish.lot_id,
                source_pond_id=pond_id,
                destiny_pond_id=None,
                fish_quantity=1,
                movement_reason="mortality",
                movement_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(mortality_movement)

            _adjust_biomass_on_movement(mortality_movement, db)
            _refresh_pond_runtime_cache_many([pond_id], db)

            fish.state = "dead"
            fish.date_of_death = now
            fish.depuration_start_time = None
            fish.updated_at = now
            db.commit()
            return go("ok", f"Pez {fish.internal_id} enviado a mortalidad.")
        except Exception:
            db.rollback()
            return go("error", "No se pudo registrar la mortalidad.")

    # ── Acción GUARDAR (normal) ───────────────────────────────────────────────
    if not (sex_changed or weight_changed or diameter_changed or development_state_changed or destination):
        return go("error", "No hay cambios para guardar.")

    now = datetime.utcnow()

    try:
        if sex_changed:
            fish.sex = target_sex

        if sex_changed or weight_changed or diameter_changed or development_state_changed:
            sample = FishSampling(
                fish_id=fish.id,
                weight=new_weight,
                diameter=new_diameter,
                development_state=target_development_state,
                registry_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(sample)

        moved = False
        if destination:
            movement = PondMovement(
                fish_id=fish.id,
                lot_id=fish.lot_id,
                source_pond_id=pond_id,
                destiny_pond_id=destination.id,
                fish_quantity=1,
                movement_reason="pond_movement",
                movement_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(movement)
            moved = True

            _adjust_biomass_on_movement(movement, db)
            _refresh_pond_runtime_cache_many([pond_id, destination.id], db)

            # Transición de estado por depuración
            if destination.depuration and not pond.depuration:
                fish.depuration_start_time = now
            if destination.depuration:
                fish.state = "depuration"
            elif pond.depuration and fish.state == "depuration":
                fish.state = "alive"
                fish.depuration_start_time = None

        fish.updated_at = now
        db.commit()

        if moved:
            return go("ok", f"Cambios guardados y pez trasladado a {destination.name}.")
        return go("ok", "Cambios guardados correctamente.")
    except Exception:
        db.rollback()
        return go("error", "No se pudieron guardar los cambios.")


# ══════════════════════════════════════════════════════════════════════════════
# MORTALIDAD PECES SIN MARCA
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/ui/ponds/{pond_id}/mortality-untagged")
def ui_pond_mortality_untagged(
    pond_id: int,
    lot_id: str = Form(...),
    quantity: int = Form(...),
    db: Session = Depends(get_db),
):
    def go(status_value: str, message: str):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status={quote_plus(status_value)}&msg={quote_plus(message)}",
            status_code=303,
        )

    if quantity < 1:
        return go("error", "La cantidad debe ser mayor a 0.")

    try:
        lot_id_int = int(lot_id)
    except ValueError:
        return go("error", "Lote no válido.")

    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        return go("error", "Estanque no encontrado.")

    # Verificar balance disponible del lote en el estanque
    balances = _get_unregistered_balances_by_lot(pond_id, db)
    available = balances.get(lot_id_int, 0)
    if available <= 0:
        return go("error", "No hay peces sin marca de ese lote en este estanque.")
    if quantity > available:
        return go("error", f"Cantidad ({quantity}) supera el saldo disponible ({available}).")

    lot = db.query(Lot).filter(Lot.id == lot_id_int).first()
    lot_label = lot.internal_id if lot and lot.internal_id else str(lot_id_int)

    now = datetime.utcnow()
    try:
        mov = PondMovement(
            fish_id=None,
            lot_id=lot_id_int,
            source_pond_id=pond_id,
            destiny_pond_id=None,
            fish_quantity=quantity,
            movement_reason="mortality",
            movement_time=now,
            created_at=now,
            updated_at=now,
        )
        db.add(mov)
        db.flush()
        _adjust_biomass_on_movement(mov, db)
        _refresh_pond_runtime_cache_many([pond_id], db)
        db.commit()
        return go("ok", f"Mortalidad registrada: {quantity} peces sin marca del lote {lot_label}.")
    except Exception:
        db.rollback()
        return go("error", "No se pudo registrar la mortalidad.")


@router.post("/ui/ponds/{pond_id}/transfer-untagged")
def ui_pond_transfer_untagged(
    pond_id: int,
    lot_id: str = Form(...),
    quantity: int = Form(...),
    destiny_pond_id: str = Form(...),
    db: Session = Depends(get_db),
):
    def go(status_value: str, message: str):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status={quote_plus(status_value)}&msg={quote_plus(message)}",
            status_code=303,
        )

    if quantity < 1:
        return go("error", "La cantidad debe ser mayor a 0.")

    try:
        lot_id_int = int(lot_id)
        dst_id = int(destiny_pond_id)
    except ValueError:
        return go("error", "Datos de traslado no válidos.")

    if dst_id == pond_id:
        return go("error", "El estanque destino debe ser distinto al actual.")

    source_pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not source_pond:
        return go("error", "Estanque origen no encontrado.")

    destiny_pond = db.query(Pond).filter(Pond.id == dst_id).first()
    if not destiny_pond:
        return go("error", "Estanque destino no encontrado.")

    source_balances = _get_unregistered_balances_by_lot(pond_id, db)
    available = source_balances.get(lot_id_int, 0)
    if available <= 0:
        return go("error", "No hay peces sin marca de ese lote en este estanque.")
    if quantity > available:
        return go("error", f"Cantidad ({quantity}) supera el saldo disponible ({available}).")

    # Regla: no mezclar lotes sin PIT tag en destino.
    destiny_balances = _get_unregistered_balances_by_lot(dst_id, db)
    active_lots = set(destiny_balances.keys())
    if active_lots and lot_id_int not in active_lots:
        lot_names = ", ".join(str(l) for l in sorted(active_lots))
        return go(
            "error",
            f"El estanque destino ya tiene peces sin PIT tag del lote(s) {lot_names}. No se pueden mezclar lotes sin registrar.",
        )

    lot = db.query(Lot).filter(Lot.id == lot_id_int).first()
    lot_label = lot.internal_id if lot and lot.internal_id else str(lot_id_int)

    now = datetime.utcnow()
    try:
        mov = PondMovement(
            fish_id=None,
            lot_id=lot_id_int,
            source_pond_id=pond_id,
            destiny_pond_id=dst_id,
            fish_quantity=quantity,
            movement_reason="pond_movement",
            movement_time=now,
            created_at=now,
            updated_at=now,
        )
        db.add(mov)
        db.flush()
        _adjust_biomass_on_movement(mov, db)
        _refresh_pond_runtime_cache_many([pond_id, dst_id], db)
        db.commit()
        return go(
            "ok",
            f"Traslado registrado: {quantity} peces sin marca del lote {lot_label} hacia {destiny_pond.name}.",
        )
    except Exception:
        db.rollback()
        return go("error", "No se pudo registrar el traslado.")


@router.post("/ui/ponds/{pond_id}/register-tagged")
def ui_pond_register_tagged(
    pond_id: int,
    internal_id: Optional[str] = Form(None),
    lot_id: Optional[str] = Form(None),
    sex: Optional[str] = Form(None),
    weight: Optional[str] = Form(None),
    diameter: Optional[str] = Form(None),
    female_destination_pond_id: Optional[str] = Form(None),
    confirm_reuse: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    redirect_base = f"/views/ui/ponds/{pond_id}"

    def go(status_value: str, message: str):
        return RedirectResponse(
            url=f"{redirect_base}?status={quote_plus(status_value)}&msg={quote_plus(message)}",
            status_code=303,
        )

    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        return go("error", "Estanque no encontrado.")

    pit_tag = _normalize_pit_tag(internal_id)
    if not pit_tag:
        return go("error", "Debe ingresar un PIT tag.")

    tag_check = _check_pit_tag_reuse(pit_tag, db)
    if tag_check["status"] == "blocked":
        return go("error", tag_check["message"])
    if tag_check["status"] == "reuse":
        if confirm_reuse != "1":
            return go("warn", tag_check["message"])
        _archive_dead_fish_tag(pit_tag, tag_check["next_idx"], db)

    available_balances = _get_unregistered_balances_by_lot(pond_id, db)
    if not available_balances:
        return go("error", "No hay saldo de peces sin PIT tag para registrar en este estanque.")

    selected_lot_id: Optional[int] = None
    if lot_id and lot_id.strip():
        if not lot_id.strip().isdigit():
            return go("error", "Lote no válido.")
        selected_lot_id = int(lot_id.strip())
    elif len(available_balances) == 1:
        selected_lot_id = next(iter(available_balances.keys()))

    if selected_lot_id is None:
        return go("error", "Debe seleccionar el lote para registrar el PIT tag.")
    if selected_lot_id not in available_balances or available_balances[selected_lot_id] < 1:
        return go("error", "No hay saldo disponible del lote seleccionado en este estanque.")

    target_sex = _normalize_sex_value(sex)

    try:
        new_weight = _parse_decimal_field(weight, "Peso")
        new_diameter = _parse_decimal_field(diameter, "Diámetro")
    except ValueError as exc:
        return go("error", str(exc))

    if new_weight is not None and new_weight <= 0:
        return go("error", "El peso debe ser mayor a 0.")
    if new_diameter is not None and new_diameter <= 0:
        return go("error", "El diámetro debe ser mayor a 0.")

    female_destination = None
    if female_destination_pond_id and female_destination_pond_id.strip():
        raw_dest = female_destination_pond_id.strip()
        if not raw_dest.isdigit():
            return go("error", "Destino de hembras no válido.")
        female_destination = db.query(Pond).filter(Pond.id == int(raw_dest)).first()
        if not female_destination:
            return go("error", "Destino de hembras no encontrado.")
        if female_destination.id == pond_id:
            return go("error", "El destino de hembras debe ser distinto al estanque actual.")

    now = datetime.utcnow()
    try:
        fish = Fish(
            internal_id=pit_tag,
            lot_id=selected_lot_id,
            sex=target_sex,
            state="depuration" if pond.depuration else "alive",
            registration_time=now,
            depuration_start_time=now if pond.depuration else None,
            created_at=now,
            updated_at=now,
        )
        db.add(fish)
        db.flush()

        out_unregistered = PondMovement(
            fish_id=None,
            lot_id=selected_lot_id,
            source_pond_id=pond_id,
            destiny_pond_id=None,
            fish_quantity=1,
            movement_reason="registration",
            movement_time=now,
            created_at=now,
            updated_at=now,
        )
        db.add(out_unregistered)

        in_registered = PondMovement(
            fish_id=fish.id,
            lot_id=selected_lot_id,
            source_pond_id=None,
            destiny_pond_id=pond_id,
            fish_quantity=1,
            movement_reason="registration",
            movement_time=now,
            created_at=now,
            updated_at=now,
        )
        db.add(in_registered)

        if new_weight is not None or new_diameter is not None:
            sample = FishSampling(
                fish_id=fish.id,
                weight=new_weight,
                diameter=new_diameter,
                registry_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(sample)

        moved_female = False
        if target_sex == "f" and female_destination:
            movement = PondMovement(
                fish_id=fish.id,
                lot_id=selected_lot_id,
                source_pond_id=pond_id,
                destiny_pond_id=female_destination.id,
                fish_quantity=1,
                movement_reason="pond_movement",
                movement_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(movement)
            moved_female = True

            # Transición de estado por depuración para el destino final de la hembra.
            if female_destination.depuration and not pond.depuration:
                fish.depuration_start_time = now
            if female_destination.depuration:
                fish.state = "depuration"
            elif pond.depuration and fish.state == "depuration":
                fish.state = "alive"
                fish.depuration_start_time = None

        fish.updated_at = now

        _adjust_biomass_on_movement(out_unregistered, db)
        _adjust_biomass_on_movement(in_registered, db)
        affected_pond_ids = {pond_id}
        if moved_female:
            _adjust_biomass_on_movement(movement, db)
            affected_pond_ids.add(female_destination.id)
        _refresh_pond_runtime_cache_many(affected_pond_ids, db)

        db.commit()
        if moved_female:
            return go("ok", f"PIT tag {pit_tag} registrado y enviado a {female_destination.name}.")
        return go("ok", f"PIT tag {pit_tag} registrado correctamente.")
    except Exception:
        db.rollback()
        return go("error", "No se pudo registrar el PIT tag.")


@router.get("/ui/fish/{fish_id}", response_class=HTMLResponse)
def ui_fish_history(
    fish_id: int,
    request: Request,
    status: Optional[str] = None,
    msg: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Historia de un pez desde su primer registro."""
    fish = db.query(Fish).filter(Fish.id == fish_id).first()
    if not fish:
        raise HTTPException(status_code=404, detail="Fish not found")

    lot = db.query(Lot).filter(Lot.id == fish.lot_id).first() if fish.lot_id else None
    pond_name_map = {p.id: p.name for p in db.query(Pond).all()}

    # Current pond: destiny of the most recent movement
    last_mv = (
        db.query(PondMovement)
        .filter(PondMovement.fish_id == fish_id)
        .order_by(PondMovement.movement_time.desc())
        .first()
    )
    current_pond_name = pond_name_map.get(last_mv.destiny_pond_id) if last_mv and last_mv.destiny_pond_id else None

    # Last fish sampling
    last_sampling = (
        db.query(FishSampling)
        .filter(FishSampling.fish_id == fish_id)
        .order_by(func.coalesce(FishSampling.registry_time, FishSampling.created_at).desc())
        .first()
    )
    last_sampling_data = None
    if last_sampling:
        w = float(last_sampling.weight) if last_sampling.weight is not None else None
        l = float(last_sampling.length) if last_sampling.length is not None else None
        k = None
        if w is not None and l is not None and l > 0:
            k = round(100 * w / (l ** 3), 4)
        ts = last_sampling.registry_time or last_sampling.created_at
        last_sampling_data = {
            "date": ts.strftime("%Y-%m-%d") if ts else None,
            "weight": f"{w:.3f}" if w is not None else None,
            "length": f"{l:.3f}" if l is not None else None,
            "k": f"{k:.4f}" if k is not None else None,
            "development_state": last_sampling.development_state or None,
        }

    sex_display = {"male": "Macho", "female": "Hembra", "macho": "Macho", "hembra": "Hembra"}.get(
        (fish.sex or "").lower(), fish.sex or "N/D"
    )

    drug_uses = (
        db.query(FishDrugUse)
        .filter(FishDrugUse.fish_id == fish_id)
        .order_by(FishDrugUse.application_date.desc(), FishDrugUse.id.desc())
        .all()
    )
    has_drug_use = len(drug_uses) > 0
    normalized_internal_id = _normalize_pit_tag(fish.internal_id)
    has_r_suffix = normalized_internal_id.endswith("_R") if normalized_internal_id else False
    needs_drug_log = has_r_suffix and not has_drug_use

    drug_rows = []
    for du in drug_uses:
        drug_rows.append({
            "drug_name": du.drug_name,
            "dose": du.dose,
            "date": du.application_date.strftime("%Y-%m-%d") if du.application_date else "",
            "withdrawal_period": du.withdrawal_period,
        })

    events = []

    registration_time = fish.registration_time or fish.created_at
    if registration_time:
        events.append({
            "time": registration_time,
            "type": "Registro",
            "detail": f"Pez registrado en lote {(lot.internal_id if lot and lot.internal_id else (lot.name if lot else 'N/D'))}",
        })

    movements = (
        db.query(PondMovement)
        .filter(PondMovement.fish_id == fish_id)
        .order_by(PondMovement.movement_time.asc())
        .all()
    )
    for mv in movements:
        src = pond_name_map.get(mv.source_pond_id, "N/D") if mv.source_pond_id else "N/A"
        dst = pond_name_map.get(mv.destiny_pond_id, "N/D") if mv.destiny_pond_id else "N/A"
        events.append({
            "time": mv.movement_time,
            "type": "Movimiento",
            "detail": f"{mv.movement_reason}: {src} -> {dst}",
        })

    female_samples = (
        db.query(FishSampling)
        .filter(FishSampling.fish_id == fish_id)
        .order_by(func.coalesce(FishSampling.registry_time, FishSampling.created_at).asc())
        .all()
    )
    for fs in female_samples:
        fs_time = fs.registry_time or fs.created_at
        detail_parts = []
        if fs.weight is not None:
            detail_parts.append(f"peso={float(fs.weight):.3f}")
        if fs.diameter is not None:
            detail_parts.append(f"diametro_caviar={float(fs.diameter):.3f}")
        if fs.oocyte_size is not None:
            detail_parts.append(f"ovocito={float(fs.oocyte_size):.3f}")
        events.append({
            "time": fs_time,
            "type": "Muestreo pez",
            "detail": ", ".join(detail_parts) if detail_parts else "sin metricas",
        })

    depuration_samples = (
        db.query(DepurationPeriodicSampling)
        .filter(DepurationPeriodicSampling.fish_id == fish_id)
        .order_by(DepurationPeriodicSampling.created_at.asc())
        .all()
    )
    for ds in depuration_samples:
        detail_parts = []
        if ds.weight is not None:
            detail_parts.append(f"peso={float(ds.weight):.3f}")
        if ds.oocyte_size is not None:
            detail_parts.append(f"ovocito={float(ds.oocyte_size):.3f}")
        if ds.pond_id is not None:
            detail_parts.append(f"estanque={pond_name_map.get(ds.pond_id, 'N/D')}")
        events.append({
            "time": ds.created_at,
            "type": "Muestreo depuracion",
            "detail": ", ".join(detail_parts) if detail_parts else "sin metricas",
        })

    for du in drug_uses:
        events.append({
            "time": du.application_date,
            "type": "Farmaco",
            "detail": f"{du.drug_name} | dosis={du.dose} | carencia={du.withdrawal_period}",
        })

    events = [e for e in events if e["time"] is not None]
    events.sort(key=lambda e: e["time"])

    template = jinja_env.get_template("fish_history.html")
    html = template.render({
        "request": request,
        "status": status,
        "msg": msg,
        "fish": {
            "id": fish.id,
            "internal_id": fish.internal_id,
            "secondary_internal_id": fish.secondary_internal_id or "",
            "notes": fish.notes or "",
            "sex": fish.sex,
            "sex_display": sex_display,
            "state": fish.state,
            "lot": (lot.internal_id if lot and lot.internal_id else (lot.name if lot else "N/D")),
            "lot_name": lot.name if lot else None,
            "can_edit_internal_id": not (has_drug_use or has_r_suffix),
            "needs_drug_log": needs_drug_log,
        },
        "current_pond": current_pond_name,
        "last_sampling": last_sampling_data,
        "drug_uses": drug_rows,
        "events": events,
    })
    return HTMLResponse(content=html)


# ── Editar internal_id de pez ───────────────────────────────────────────────
@router.post("/ui/fish/{fish_id}/edit-internal-id")
def ui_fish_edit_internal_id(
    fish_id: int,
    new_internal_id: str = Form(...),
    db: Session = Depends(get_db)
):
    """Actualiza el internal_id (PIT tag) de un pez."""
    new_val = new_internal_id.strip().upper()
    if not new_val:
        return JSONResponse({"ok": False, "error": "El PIT tag no puede estar vacío"}, status_code=422)
    if new_val.endswith("_R"):
        return JSONResponse({"ok": False, "error": "El sufijo _R se asigna solo mediante registro de fármacos."}, status_code=422)

    fish = db.query(Fish).filter(Fish.id == fish_id).first()
    if not fish:
        return JSONResponse({"ok": False, "error": "Pez no encontrado"}, status_code=404)

    has_drug_use = db.query(FishDrugUse.id).filter(FishDrugUse.fish_id == fish_id).first() is not None
    normalized_current_id = _normalize_pit_tag(fish.internal_id)
    has_r_suffix = normalized_current_id.endswith("_R") if normalized_current_id else False
    if has_drug_use or has_r_suffix:
        return JSONResponse({"ok": False, "error": "PIT bloqueado por trazabilidad de fármacos (_R)."}, status_code=409)

    # Check uniqueness (excluding itself)
    duplicate = db.query(Fish).filter(Fish.internal_id == new_val, Fish.id != fish_id).first()
    if duplicate:
        return JSONResponse({"ok": False, "error": f"Ya existe un pez con PIT tag '{new_val}'"}, status_code=409)

    fish.internal_id = new_val
    fish.updated_at = datetime.utcnow()
    db.commit()
    return JSONResponse({"ok": True, "internal_id": new_val})


@router.post("/ui/fish/{fish_id}/drug-use")
def ui_fish_add_drug_use(
    fish_id: int,
    drug_name: str = Form(...),
    dose: str = Form(...),
    application_date: str = Form(...),
    withdrawal_period: str = Form(...),
    db: Session = Depends(get_db),
):
    def go(status_value: str, message: str):
        return RedirectResponse(
            url=f"/views/ui/fish/{fish_id}?status={quote_plus(status_value)}&msg={quote_plus(message)}",
            status_code=303,
        )

    fish = db.query(Fish).filter(Fish.id == fish_id).first()
    if not fish:
        return go("error", "Pez no encontrado.")

    drug_name_val = (drug_name or "").strip()
    dose_val = (dose or "").strip()
    withdrawal_val = (withdrawal_period or "").strip()
    date_raw = (application_date or "").strip()

    if not drug_name_val or not dose_val or not withdrawal_val or not date_raw:
        return go("error", "Debe completar fármaco, dosis, fecha y carencia.")

    try:
        application_dt = datetime.strptime(date_raw, "%Y-%m-%d")
    except ValueError:
        return go("error", "Fecha inválida para el registro de fármaco.")

    now = datetime.utcnow()
    log = FishDrugUse(
        fish_id=fish_id,
        drug_name=drug_name_val,
        dose=dose_val,
        application_date=application_dt,
        withdrawal_period=withdrawal_val,
        created_at=now,
        updated_at=now,
    )
    db.add(log)

    normalized_current = _normalize_pit_tag(fish.internal_id)
    if not normalized_current:
        db.rollback()
        return go("error", "El pez no tiene PIT tag válido para asignar sufijo _R.")

    if not normalized_current.endswith("_R"):
        candidate_internal_id = f"{normalized_current}_R"
        if len(candidate_internal_id) > 120:
            db.rollback()
            return go("error", "No se pudo asignar _R: PIT tag excede largo máximo.")

        duplicate = (
            db.query(Fish.id)
            .filter(func.upper(func.trim(Fish.internal_id)) == candidate_internal_id, Fish.id != fish_id)
            .first()
        )
        if duplicate:
            db.rollback()
            return go("error", f"No se pudo asignar _R: ya existe el PIT tag {candidate_internal_id} en otro pez.")
        fish.internal_id = candidate_internal_id
    else:
        fish.internal_id = normalized_current

    fish.updated_at = now
    db.commit()
    return go("ok", "Fármaco registrado. PIT tag bloqueado con sufijo _R.")


@router.post("/ui/fish/{fish_id}/edit-secondary-internal-id")
def ui_fish_edit_secondary_internal_id(
    fish_id: int,
    new_secondary_internal_id: str = Form(...),
    db: Session = Depends(get_db)
):
    """Actualiza el PIT tag secundario de un pez."""
    new_val = new_secondary_internal_id.strip().upper()
    fish = db.query(Fish).filter(Fish.id == fish_id).first()
    if not fish:
        return JSONResponse({"ok": False, "error": "Pez no encontrado"}, status_code=404)
    fish.secondary_internal_id = new_val or None
    fish.updated_at = datetime.utcnow()
    db.commit()
    return JSONResponse({"ok": True, "secondary_internal_id": new_val})


@router.post("/ui/fish/{fish_id}/edit-notes")
def ui_fish_edit_notes(
    fish_id: int,
    notes: str = Form(...),
    db: Session = Depends(get_db)
):
    """Actualiza las notas de un pez."""
    new_val = notes.strip()
    fish = db.query(Fish).filter(Fish.id == fish_id).first()
    if not fish:
        return JSONResponse({"ok": False, "error": "Pez no encontrado"}, status_code=404)
    fish.notes = new_val or None
    fish.updated_at = datetime.utcnow()
    db.commit()
    return JSONResponse({"ok": True, "notes": new_val})


# ── Búsqueda de pez por PIT tag (AJAX) ─────────────────────────────────────
@router.get("/ui/fish/check-pit-tag")
def fish_check_pit_tag(tag: str, db: Session = Depends(get_db)):
    """AJAX: valida si un PIT tag puede registrarse (libre, bloqueado, o reutilizable)."""
    pit_tag = _normalize_pit_tag(tag)
    if not pit_tag:
        return JSONResponse({"status": "blocked", "message": "Tag vacío."})
    return JSONResponse(_check_pit_tag_reuse(pit_tag, db))


@router.get("/ui/fish-by-pit")
def ui_fish_find_by_pit(
    pit_tag: Optional[str] = None,
    next: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Busca por PIT tag y redirige a la ficha del pez."""
    cleaned = _normalize_pit_tag(pit_tag)
    next_path = _safe_ui_next_path(next)

    def go_error(message: str):
        return RedirectResponse(
            url=f"{next_path}?status=error&msg={quote_plus(message)}",
            status_code=303,
        )

    if not cleaned:
        return go_error("Ingrese un PIT tag para buscar.")

    fish = (
        db.query(Fish)
        .filter(func.upper(func.trim(Fish.internal_id)) == cleaned)
        .order_by(Fish.id.desc())
        .first()
    )
    if not fish:
        return go_error(f"No se encontró el PIT tag {cleaned}.")

    return RedirectResponse(url=f"/views/ui/fish/{fish.id}", status_code=303)


@router.get("/ui/fish-search")
def fish_search(q: str, db: Session = Depends(get_db)):
    """Busca un pez por internal_id y retorna su info + estanque actual."""
    pit_tag = _normalize_pit_tag(q)
    fish = db.query(Fish).filter(Fish.internal_id == pit_tag).first()
    if not fish:
        return JSONResponse({"found": False})

    lot = db.query(Lot).filter(Lot.id == fish.lot_id).first() if fish.lot_id else None

    # Estanque actual: último movimiento por id
    last_movement = (
        db.query(PondMovement)
        .filter(PondMovement.fish_id == fish.id)
        .order_by(PondMovement.movement_time.desc(), PondMovement.id.desc())
        .first()
    )
    current_pond = None
    if last_movement and last_movement.destiny_pond_id:
        pond = db.query(Pond).filter(Pond.id == last_movement.destiny_pond_id).first()
        current_pond = pond.name if pond else None

    return JSONResponse({
        "found": True,
        "id": fish.id,
        "internal_id": fish.internal_id,
        "state": fish.state,
        "lot": lot.internal_id if lot else "N/D",
        "lot_id": fish.lot_id,
        "current_pond": current_pond,
    })


@router.get("/ui/ponds/{pond_id}/pit-tags")
def ui_pond_pit_tags(pond_id: int, db: Session = Depends(get_db)):
    """Retorna PIT tags actuales del estanque para autocompletar en UI."""
    fish_list = _get_current_tagged_fish_in_pond(pond_id, db)
    pit_tags = [f.internal_id for f in fish_list if f.internal_id]
    return JSONResponse({"pit_tags": pit_tags})


# ── Formulario de nuevo movimiento ─────────────────────────────────────────
@router.get("/ui/movements/new", response_class=HTMLResponse)
def ui_movement_new(
    request: Request,
    source_pond_id: Optional[int] = None,
    mode: Optional[str] = None,
    tagged_batch: bool = False,
    db: Session = Depends(get_db),
):
    ponds = db.query(Pond).order_by(Pond.name).all()
    lots = db.query(Lot).order_by(Lot.name).all()

    # Pre-calcular saldos de peces sin PIT tag por estanque/lote para el JS
    pond_balances: dict[int, dict[int, int]] = {}
    for pond in ponds:
        balances = _get_unregistered_balances_by_lot(pond.id, db)
        if balances:
            pond_balances[pond.id] = balances

    prefill_pit_tags: list[str] = []
    if source_pond_id:
        prefill_pit_tags = [
            f.internal_id
            for f in _get_current_tagged_fish_in_pond(source_pond_id, db)
            if f.internal_id
        ]

    template = jinja_env.get_template("movement_form.html")
    html = template.render({
        "request": request,
        "ponds": ponds,
        "lots": lots,
        "pond_balances_json": json.dumps(pond_balances),
        "prefill_source": source_pond_id,
        "prefill_destiny": None,
        "prefill_pit_tags": prefill_pit_tags,
        "prefill_mode": mode if mode in ("tagged", "untagged") else "tagged",
        "prefill_tagged_batch": bool(tagged_batch),
        "error": None,
        "success": None,
    })
    return HTMLResponse(content=html)


# ── POST del formulario ─────────────────────────────────────────────────────
ALLOWED_REASONS = [
    "mortality", "depuration", "inventory_mismatch", "registration", "devious",
    "first_load", "pond_movement", "unmarked_devious", "missing_number",
]

@router.post("/ui/movements", response_class=HTMLResponse)
def ui_movement_create(
    request: Request,
    mode: str = Form(...),
    movement_reason: str = Form(...),
    movement_time: str = Form(...),
    source_pond_id: Optional[str] = Form(None),
    destiny_pond_id: Optional[str] = Form(None),
    fish_id: Optional[str] = Form(None),
    tagged_batch_ids: Optional[str] = Form(None),
    lot_id: Optional[str] = Form(None),
    fish_quantity_tagged: int = Form(1),
    fish_quantity_untagged: Optional[int] = Form(None),
    post_action: str = Form("go_source"),
    db: Session = Depends(get_db),
):
    ponds = db.query(Pond).order_by(Pond.name).all()
    lots = db.query(Lot).order_by(Lot.name).all()
    pond_balances: dict[int, dict[int, int]] = {}
    for pond in ponds:
        balances = _get_unregistered_balances_by_lot(pond.id, db)
        if balances:
            pond_balances[pond.id] = balances

    def render_form(error=None, success=None):
        prefill_source_id = int(source_pond_id) if source_pond_id else None
        prefill_pit_tags: list[str] = []
        if prefill_source_id:
            prefill_pit_tags = [
                f.internal_id
                for f in _get_current_tagged_fish_in_pond(prefill_source_id, db)
                if f.internal_id
            ]
        template = jinja_env.get_template("movement_form.html")
        return HTMLResponse(template.render({
            "request": request,
            "ponds": ponds,
            "lots": lots,
            "pond_balances_json": json.dumps(pond_balances),
            "prefill_source": prefill_source_id,
            "prefill_destiny": int(destiny_pond_id) if destiny_pond_id else None,
            "prefill_pit_tags": prefill_pit_tags,
            "prefill_mode": mode if mode in ("tagged", "untagged") else "tagged",
            "prefill_tagged_batch": bool(tagged_batch_ids and tagged_batch_ids.strip()),
            "error": error,
            "success": success,
        }))

    def go_after_save(status_value: str, message: str):
        target_pond_id = None
        if post_action == "go_dest" and dst_id:
            target_pond_id = dst_id
        elif post_action == "go_source" and src_id:
            target_pond_id = src_id
        elif dst_id:
            target_pond_id = dst_id
        elif src_id:
            target_pond_id = src_id

        if target_pond_id:
            return RedirectResponse(
                url=f"/views/ui/ponds/{target_pond_id}?status={quote_plus(status_value)}&msg={quote_plus(message)}",
                status_code=303,
            )

        if status_value == "ok":
            return render_form(success=message)
        return render_form(error=message)

    # Validaciones básicas
    if movement_reason not in ALLOWED_REASONS:
        return render_form(error="Razón de movimiento no válida.")

    try:
        mov_time = datetime.fromisoformat(movement_time)
    except ValueError:
        return render_form(error="Fecha/hora no válida.")

    src_id = int(source_pond_id) if source_pond_id and source_pond_id.strip() else None
    dst_id = int(destiny_pond_id) if destiny_pond_id and destiny_pond_id.strip() else None

    if src_id and dst_id and src_id == dst_id:
        return render_form(error="El estanque origen y destino no pueden ser el mismo.")

    if mode == "tagged":
        # Movimiento masivo de peces con PIT tag
        batch_ids: list[int] = []
        if tagged_batch_ids and tagged_batch_ids.strip():
            for raw in tagged_batch_ids.split(","):
                item = raw.strip()
                if not item:
                    continue
                if not item.isdigit():
                    return render_form(error=f"ID de pez inválido en lote masivo: {item}")
                fid = int(item)
                if fid not in batch_ids:
                    batch_ids.append(fid)

        if batch_ids:
            if not src_id:
                return render_form(error="Debe indicar estanque origen para movimiento masivo con PIT tag.")

            src_pond = db.query(Pond).filter(Pond.id == src_id).first() if src_id else None
            dest_pond = db.query(Pond).filter(Pond.id == dst_id).first() if dst_id else None

            movable_fish: list[Fish] = []
            failed_pits: list[str] = []
            moved_pits: list[str] = []

            for fish_id_int in batch_ids:
                fish = db.query(Fish).filter(Fish.id == fish_id_int).first()
                if not fish:
                    failed_pits.append(f"id={fish_id_int} (no encontrado)")
                    continue

                pit = fish.internal_id or str(fish.id)
                if fish.state not in ("alive", "depuration"):
                    failed_pits.append(f"{pit} (inactivo: {fish.state})")
                    continue

                last_movement = (
                    db.query(PondMovement)
                    .filter(PondMovement.fish_id == fish.id)
                    .order_by(PondMovement.movement_time.desc(), PondMovement.id.desc())
                    .first()
                )
                if not last_movement or last_movement.destiny_pond_id != src_id:
                    failed_pits.append(f"{pit} (no está en origen)")
                    continue

                movable_fish.append(fish)

            if not movable_fish:
                detail = "; ".join(failed_pits[:20])
                if len(failed_pits) > 20:
                    detail += f" ... (+{len(failed_pits)-20})"
                return render_form(error=f"No se movieron peces con PIT. Fallidos: {detail}")

            try:
                for fish in movable_fish:
                    new_mov = PondMovement(
                        fish_id=fish.id,
                        lot_id=fish.lot_id,
                        source_pond_id=src_id,
                        destiny_pond_id=dst_id,
                        fish_quantity=1,
                        movement_reason=movement_reason,
                        movement_time=mov_time,
                    )
                    db.add(new_mov)
                    db.flush()

                    # Transición de estado por depuración
                    if dst_id and dest_pond and dest_pond.depuration:
                        fish.state = "depuration"
                        if not (src_pond and src_pond.depuration):
                            fish.depuration_start_time = mov_time
                    if src_pond and src_pond.depuration and dst_id and dest_pond and not dest_pond.depuration:
                        fish.state = "alive"
                        fish.depuration_start_time = None

                    _adjust_biomass_on_movement(new_mov, db)
                    moved_pits.append(fish.internal_id or str(fish.id))

                _refresh_pond_runtime_cache_many([src_id, dst_id], db)
                db.commit()
            except Exception:
                db.rollback()
                return render_form(error="No se pudo registrar el movimiento masivo con PIT tag.")

            dest_label = dest_pond.name if dst_id and dest_pond else "egreso"
            msg = f"Peces movidos con éxito: {len(moved_pits)} hacia {dest_label}."
            if failed_pits:
                failed_detail = "; ".join(failed_pits[:20])
                if len(failed_pits) > 20:
                    failed_detail += f" ... (+{len(failed_pits)-20})"
                msg += f" No movidos: {failed_detail}"
            return go_after_save("ok", msg)

        # Pez con PIT tag
        if not fish_id or not fish_id.strip():
            return render_form(error="Debes seleccionar un pez con PIT tag válido.")
        fish_id_int = int(fish_id)
        fish = db.query(Fish).filter(Fish.id == fish_id_int).first()
        if not fish:
            return render_form(error="Pez no encontrado.")
        if fish.state not in ("alive", "depuration"):
            return render_form(error=f"El pez {fish.internal_id} no está activo (estado: {fish.state}).")

        lot_id_int = fish.lot_id

        new_mov = PondMovement(
            fish_id=fish_id_int,
            lot_id=lot_id_int,
            source_pond_id=src_id,
            destiny_pond_id=dst_id,
            fish_quantity=1,
            movement_reason=movement_reason,
            movement_time=mov_time,
        )
        db.add(new_mov)
        db.flush()

        # Transición de estado por depuración
        src_pond = db.query(Pond).filter(Pond.id == src_id).first() if src_id else None
        dest_pond = db.query(Pond).filter(Pond.id == dst_id).first() if dst_id else None

        if dst_id:
            if dest_pond and dest_pond.depuration:
                fish.state = "depuration"
                # Inicia ciclo solo en transición no-depuración -> depuración.
                if not (src_pond and src_pond.depuration):
                    fish.depuration_start_time = mov_time
        if src_pond and src_pond.depuration and dst_id and dest_pond and not dest_pond.depuration:
            fish.state = "alive"
            fish.depuration_start_time = None

        _adjust_biomass_on_movement(new_mov, db)
        _refresh_pond_runtime_cache_many([src_id, dst_id], db)
        db.commit()
        return go_after_save("ok", f"Peces movidos con éxito: 1 ({fish.internal_id}) hacia {dest_pond.name if dst_id else 'egreso'}.")

    else:
        # Peces sin PIT tag (masivo)
        lot_id_str = lot_id if lot_id and lot_id.strip() else None
        if not lot_id_str:
            return render_form(error="Debes seleccionar un lote para movimiento masivo.")
        if not fish_quantity_untagged or fish_quantity_untagged < 1:
            return render_form(error="La cantidad de peces debe ser mayor a 0.")

        lot_id_int = int(lot_id_str)

        # Regla: no mezclar lotes sin PIT tag en destino
        if dst_id:
            balances = _get_unregistered_balances_by_lot(dst_id, db)
            active_lots = set(balances.keys())
            if active_lots and lot_id_int not in active_lots:
                lot_names = ", ".join(str(l) for l in sorted(active_lots))
                return render_form(
                    error=f"El estanque destino ya tiene peces sin PIT tag del lote(s) {lot_names}. "
                          "No se pueden mezclar lotes sin registrar."
                )

        new_mov = PondMovement(
            fish_id=None,
            lot_id=lot_id_int,
            source_pond_id=src_id,
            destiny_pond_id=dst_id,
            fish_quantity=fish_quantity_untagged,
            movement_reason=movement_reason,
            movement_time=mov_time,
        )
        db.add(new_mov)
        db.flush()
        _adjust_biomass_on_movement(new_mov, db)
        _refresh_pond_runtime_cache_many([src_id, dst_id], db)
        db.commit()

        lot = db.query(Lot).filter(Lot.id == lot_id_int).first()
        lot_label = lot.internal_id if lot else str(lot_id_int)
        return go_after_save("ok", f"Peces movidos con éxito: {fish_quantity_untagged} sin PIT (lote {lot_label}).")


# ══════════════════════════════════════════════════════════════════════════════
# MUESTREO RUTINARIO
# ══════════════════════════════════════════════════════════════════════════════

def _get_fish_weight_estimate(fish_id: int, lot_id: int, db: Session) -> Optional[float]:
    """Último peso individual del pez; fallback = avg peces marcados del mismo lote."""
    row = (
        db.query(FishSampling.weight)
        .filter(FishSampling.fish_id == fish_id, FishSampling.weight.isnot(None))
        .order_by(func.coalesce(FishSampling.registry_time, FishSampling.created_at).desc())
        .first()
    )
    if row:
        return float(row[0])
    # fallback: avg marcados del lote
    row2 = (
        db.query(func.avg(FishSampling.weight))
        .join(Fish, Fish.id == FishSampling.fish_id)
        .filter(Fish.lot_id == lot_id, FishSampling.weight.isnot(None))
        .first()
    )
    return float(row2[0]) if row2 and row2[0] else None


def _get_lot_weight_estimate(pond_id: Optional[int], lot_id: int, db: Session) -> Optional[float]:
    """avg_weight de pond_lot_stats (pond+lote); fallback = último avg del lote en cualquier estanque."""
    if pond_id:
        row = (
            db.query(PondLotStats.avg_weight)
            .filter(PondLotStats.pond_id == pond_id, PondLotStats.lot_id == lot_id,
                    PondLotStats.avg_weight.isnot(None))
            .first()
        )
        if row:
            return float(row[0])
    # fallback global del lote
    row2 = (
        db.query(PondLotStats.avg_weight)
        .filter(PondLotStats.lot_id == lot_id, PondLotStats.avg_weight.isnot(None))
        .order_by(PondLotStats.updated_at.desc().nullslast())
        .first()
    )
    return float(row2[0]) if row2 and row2[0] else None


def _adjust_biomass_on_movement(mov: PondMovement, db: Session) -> None:
    """
    Ajusta biomass_current de los ponds afectados por un movimiento.

    Pez marcado (fish_id IS NOT NULL):
      - Entre estanques: transfiere peso individual de A → B
      - Egreso (sin destiny): elimina peso de A
      - Biomasa de lote: no cambia (los marcados se tracean individualmente)

    Pez sin marcar (fish_id IS NULL):
      - Entre estanques: resta N×avg_weight_A de A, suma N×avg_weight_A a B
        · avg_weight de A no cambia
        · avg_weight de B se actualiza como promedio ponderado
      - Egreso (sin destiny): elimina N×avg_weight_A de A
      - Biomasa nunca queda negativa
    """
    # ── Razones que implican egreso definitivo (sin destiny esperado) ──
    EXIT_REASONS = {"mortality", "devious", "missing_number", "unmarked_devious"}

    if mov.fish_id:
        # ── TAGGED ──
        fish = db.query(Fish).filter(Fish.id == mov.fish_id).first()
        lot_id = fish.lot_id if fish else mov.lot_id
        peso_g = _get_fish_weight_estimate(mov.fish_id, lot_id, db)
        if peso_g is None:
            return
        delta_kg = Decimal(str(peso_g)) / 1000

        if mov.source_pond_id:
            src = db.query(Pond).filter(Pond.id == mov.source_pond_id).first()
            if src and src.biomass_current is not None:
                src.biomass_current = max(Decimal("0"), Decimal(str(src.biomass_current)) - delta_kg)
                src.updated_at = datetime.utcnow()

        if mov.destiny_pond_id:
            dst = db.query(Pond).filter(Pond.id == mov.destiny_pond_id).first()
            if dst is not None:
                cur = Decimal(str(dst.biomass_current)) if dst.biomass_current is not None else Decimal("0")
                dst.biomass_current = cur + delta_kg
                dst.updated_at = datetime.utcnow()

    else:
        # ── UNTAGGED ──
        qty = int(mov.fish_quantity or 1)
        if qty <= 0:
            return

        # avg_weight desde el estanque origen (o fallback del lote si es ingreso desde fuera)
        ref_pond_id = mov.source_pond_id if mov.source_pond_id else mov.destiny_pond_id
        avg_g = _get_lot_weight_estimate(ref_pond_id, mov.lot_id, db)
        if avg_g is None:
            return

        delta_kg = Decimal(str(avg_g)) * qty / 1000

        # ── Estanque origen ──
        if mov.source_pond_id:
            src = db.query(Pond).filter(Pond.id == mov.source_pond_id).first()
            if src and src.biomass_current is not None:
                src.biomass_current = max(Decimal("0"), Decimal(str(src.biomass_current)) - delta_kg)
                src.updated_at = datetime.utcnow()
                # avg_weight de origen NO cambia (los peces restantes tienen el mismo peso prom.)

        # ── Estanque destino ──
        if mov.destiny_pond_id:
            dst = db.query(Pond).filter(Pond.id == mov.destiny_pond_id).first()
            if dst is not None:
                cur = Decimal(str(dst.biomass_current)) if dst.biomass_current is not None else Decimal("0")
                dst.biomass_current = cur + delta_kg
                dst.updated_at = datetime.utcnow()

                # Actualizar avg_weight del lote en destino (promedio ponderado)
                # Nota: tras flush(), el balance ya incluye el movimiento actual,
                # por eso restamos qty para obtener el balance previo.
                balances_post = _get_unregistered_balances_by_lot(mov.destiny_pond_id, db)
                balance_post = balances_post.get(mov.lot_id, qty)
                pre_balance = max(0, balance_post - qty)

                existing_avg_g = _get_lot_weight_estimate(mov.destiny_pond_id, mov.lot_id, db) or avg_g

                if pre_balance > 0:
                    new_avg_g = (pre_balance * existing_avg_g + qty * avg_g) / (pre_balance + qty)
                else:
                    new_avg_g = avg_g

                pls = (
                    db.query(PondLotStats)
                    .filter(PondLotStats.pond_id == mov.destiny_pond_id,
                            PondLotStats.lot_id == mov.lot_id)
                    .first()
                )
                if pls:
                    pls.avg_weight = round(new_avg_g, 3)
                    pls.updated_at = datetime.utcnow()
                else:
                    db.add(
                        PondLotStats(
                            pond_id=mov.destiny_pond_id,
                            lot_id=mov.lot_id,
                            avg_weight=round(new_avg_g, 3),
                            n_sampled=0,
                            updated_at=datetime.utcnow(),
                        )
                    )


def _recalc_pond_biomass(pond_id: int, db: Session) -> None:
    """
    Recalcula biomass y avg_weight en ponds usando:
    - Peces marcados: último peso individual de FishSampling (o fish.weight si no tiene)
    - Peces sin marcar por lote: avg_weight de pond_lot_stats
    Actualiza ponds.biomass y ponds.avg_weight en la misma transacción.
    """
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        return

    tagged_fish = _get_current_tagged_fish_in_pond(pond_id, db)
    unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)

    def _recent_lot_weight_floor_map() -> dict[int, float]:
        """Promedio reciente por lote dentro del estanque para corregir pesos individuales antiguos."""
        lot_ids = sorted({int(f.lot_id) for f in tagged_fish if f.lot_id is not None})
        if not lot_ids:
            return {}

        cutoff = datetime.utcnow() - timedelta(days=RECENT_LOT_WEIGHT_LOOKBACK_DAYS)
        rows = (
            db.query(
                SamplingRecord.lot_id,
                func.avg(SamplingRecord.weight).label("avg_w"),
                func.count(SamplingRecord.id).label("n"),
            )
            .join(SamplingSession, SamplingSession.id == SamplingRecord.session_id)
            .filter(
                SamplingSession.pond_id == pond_id,
                SamplingRecord.lot_id.in_(lot_ids),
                SamplingRecord.weight.isnot(None),
                func.coalesce(SamplingSession.closed_at, SamplingSession.created_at) >= cutoff,
            )
            .group_by(SamplingRecord.lot_id)
            .all()
        )
        return {
            int(r.lot_id): float(r.avg_w)
            for r in rows
            if r.lot_id and r.avg_w and int(r.n or 0) >= MIN_RECENT_LOT_SAMPLES_FOR_FLOOR
        }

    lot_recent_floor = _recent_lot_weight_floor_map()

    # Peso de peces marcados: última muestra individual
    tagged_ids = [f.id for f in tagged_fish]
    latest_weight_by_fish: dict[int, float] = {}
    latest_time_by_fish: dict[int, datetime] = {}
    if tagged_ids:
        latest_subq = (
            db.query(
                FishSampling.fish_id,
                func.max(
                    func.coalesce(FishSampling.registry_time, FishSampling.created_at)
                ).label("max_time"),
            )
            .filter(FishSampling.fish_id.in_(tagged_ids))
            .group_by(FishSampling.fish_id)
            .subquery()
        )
        rows = (
            db.query(FishSampling)
            .join(
                latest_subq,
                (FishSampling.fish_id == latest_subq.c.fish_id) &
                (func.coalesce(FishSampling.registry_time, FishSampling.created_at) == latest_subq.c.max_time),
            )
            .all()
        )
        for row in rows:
            if row.weight is not None:
                latest_weight_by_fish[row.fish_id] = float(row.weight)
                sample_dt = row.registry_time or row.created_at
                if sample_dt:
                    latest_time_by_fish[row.fish_id] = sample_dt

    tagged_biomass = Decimal("0")
    now_utc = datetime.utcnow()
    for f in tagged_fish:
        w = latest_weight_by_fish.get(f.id)
        # Si no hay peso individual, mantiene fallback existente.
        if w is None:
            w = _get_fish_weight_estimate(f.id, f.lot_id, db)

        # Corrección de estancamiento: si el último peso es antiguo,
        # no permitir que quede por debajo del promedio reciente del lote.
        last_dt = latest_time_by_fish.get(f.id)
        floor_w = lot_recent_floor.get(int(f.lot_id)) if f.lot_id else None
        if w is not None and last_dt and floor_w is not None:
            age_days = (now_utc - last_dt).days
            if age_days > STALE_WEIGHT_DAYS:
                w = max(w, floor_w)

        if w:
            tagged_biomass += Decimal(str(w))

    # Peso de peces sin marcar: usa avg_weight de pond_lot_stats por lote
    unregistered_biomass = Decimal("0")
    lot_ids_unreg = list(unregistered_balances.keys())
    stats_rows = (
        db.query(PondLotStats)
        .filter(PondLotStats.pond_id == pond_id, PondLotStats.lot_id.in_(lot_ids_unreg))
        .all()
    ) if lot_ids_unreg else []
    stats_by_lot = {s.lot_id: s for s in stats_rows}

    for lot_id, qty in unregistered_balances.items():
        stats = stats_by_lot.get(lot_id)
        if stats and stats.avg_weight and qty > 0:
            unregistered_biomass += Decimal(str(stats.avg_weight)) * qty

    total_biomass = tagged_biomass + unregistered_biomass  # gramos
    total_population = len(tagged_fish) + sum(unregistered_balances.values())

    biomass_kg = round(total_biomass / 1000, 3) if total_biomass else None
    avg_w = round(total_biomass / total_population, 3) if total_population > 0 and total_biomass else None

    pond.biomass = biomass_kg
    pond.biomass_measured = biomass_kg   # snapshot del muestreo
    pond.biomass_current = biomass_kg    # resetea el running total
    pond.avg_weight = avg_w

    tagged_lot_ids = {int(f.lot_id) for f in tagged_fish if f.lot_id is not None}
    unregistered_lot_ids = {int(lot_id) for lot_id in unregistered_balances.keys()}
    active_lot_ids = sorted(tagged_lot_ids | unregistered_lot_ids)

    pond.tagged_count = len(tagged_fish)
    pond.unregistered_count = int(sum(unregistered_balances.values()))
    pond.n_fish_cached = pond.tagged_count + pond.unregistered_count
    pond.active_lots_count = len(active_lot_ids)
    pond.active_lot_ids = active_lot_ids
    pond.unregistered_lot_ids = sorted(unregistered_lot_ids)
    pond.unregistered_lot_conflict = len(unregistered_lot_ids) > 1
    pond.updated_at = datetime.utcnow()


def _close_sampling_session(session: SamplingSession, db: Session) -> None:
    """
    Cierra la sesión de muestreo:
    1. Calcula avg_weight, avg_length y K por (pond, lot) desde los registros SIN PIT
    2. Hace UPSERT en pond_lot_stats
    3. Recalcula biomasa del estanque
    4. Marca la sesión como cerrada
    """
    now = datetime.utcnow()

    # Registros sin PIT (fish_id IS NULL) → representativos de la población sin marcar
    records_untagged = (
        db.query(SamplingRecord)
        .filter(
            SamplingRecord.session_id == session.id,
            SamplingRecord.fish_id.is_(None),
            SamplingRecord.lot_id.isnot(None),
        )
        .all()
    )

    # Agrupar por lot_id
    from collections import defaultdict
    by_lot: dict = defaultdict(list)
    for r in records_untagged:
        by_lot[r.lot_id].append(r)

    for lot_id, recs in by_lot.items():
        weights = [float(r.weight) for r in recs if r.weight is not None]
        lengths = [float(r.length) for r in recs if r.length is not None]
        n = len(recs)
        avg_w = sum(weights) / len(weights) if weights else None
        avg_l = sum(lengths) / len(lengths) if lengths else None

        # K = (peso_g / (longitud_cm)³) × 100
        k = None
        if avg_w and avg_l and avg_l > 0:
            length_cm = avg_l / 10.0
            k = round((avg_w / (length_cm ** 3)) * 100, 4)

        existing = (
            db.query(PondLotStats)
            .filter(PondLotStats.pond_id == session.pond_id, PondLotStats.lot_id == lot_id)
            .first()
        )
        if existing:
            existing.avg_weight = avg_w
            existing.avg_length = avg_l
            existing.condition_k = k
            existing.n_sampled = n
            existing.last_sampling_session_id = session.id
            existing.updated_at = now
        else:
            db.add(PondLotStats(
                pond_id=session.pond_id,
                lot_id=lot_id,
                avg_weight=avg_w,
                avg_length=avg_l,
                condition_k=k,
                n_sampled=n,
                last_sampling_session_id=session.id,
                updated_at=now,
            ))

    # Recalcular biomasa del estanque
    _recalc_pond_biomass(session.pond_id, db)

    # Marcar sesión como cerrada
    session.closed_at = now
    session.updated_at = now


def _get_sampling_session_context(session: SamplingSession, db: Session) -> dict:
    """Construye el contexto completo de una sesión de muestreo para el template."""
    pond = db.query(Pond).filter(Pond.id == session.pond_id).first()

    # Peces con PIT activos en el estanque para datalist
    tagged_fish = _get_current_tagged_fish_in_pond(session.pond_id, db)
    fish_options = [{"id": f.id, "pit": f.internal_id} for f in tagged_fish]

    # Lotes disponibles (de peces registrados y no registrados)
    lot_ids_registered = list({f.lot_id for f in tagged_fish if f.lot_id})
    unregistered_balances = _get_unregistered_balances_by_lot(session.pond_id, db)
    lot_ids_unregistered = list(unregistered_balances.keys())
    all_lot_ids = list(set(lot_ids_registered + lot_ids_unregistered))
    lots_map = {
        l.id: l for l in db.query(Lot).filter(Lot.id.in_(all_lot_ids)).all()
    } if all_lot_ids else {}

    lot_options = []
    for lot_id in all_lot_ids:
        lot = lots_map.get(lot_id)
        label = lot.internal_id if lot and lot.internal_id else (lot.name if lot else str(lot_id))
        lot_options.append({"id": lot_id, "label": label})
    lot_options.sort(key=lambda x: x["label"])

    # Registros existentes de esta sesión
    records_db = (
        db.query(SamplingRecord)
        .filter(SamplingRecord.session_id == session.id)
        .order_by(SamplingRecord.id.desc())
        .all()
    )

    # Mapa pit → fish para mostrar en filas
    fish_by_id = {f.id: f for f in tagged_fish}

    records = []
    total_weight = Decimal("0")
    for r in records_db:
        fish_obj = fish_by_id.get(r.fish_id) if r.fish_id else None
        pit_display = fish_obj.internal_id if fish_obj else ""
        lot_obj = lots_map.get(r.lot_id) if r.lot_id else None
        lot_label = lot_obj.internal_id if lot_obj and lot_obj.internal_id else (lot_obj.name if lot_obj else "")
        w = float(r.weight) if r.weight is not None else None
        l = float(r.length) if r.length is not None else None
        records.append({
            "id": r.id,
            "pit": pit_display,
            "fish_id": r.fish_id,
            "lot_id": r.lot_id,
            "lot_label": lot_label,
            "weight": w,
            "length": l,
            "notes": r.notes or "",
        })
        if r.weight is not None:
            total_weight += Decimal(str(r.weight))

    # Total población del estanque
    total_population = len(tagged_fish) + sum(unregistered_balances.values())
    pct_sampled = round(len(records) / total_population * 100, 1) if total_population > 0 else 0

    # Biomasa estimada de esta sesión: peso promedio × total_population
    avg_weight = float(total_weight / len(records)) if records else None
    estimated_biomass = round(avg_weight * total_population / 1000, 2) if avg_weight else None  # kg

    # pond_lot_stats vigentes (post cierre si existen)
    lot_stats_rows = (
        db.query(PondLotStats)
        .filter(PondLotStats.pond_id == session.pond_id)
        .all()
    )
    lot_stats = [
        {
            "lot_label": lots_map.get(s.lot_id, {}) and (
                lots_map[s.lot_id].internal_id or lots_map[s.lot_id].name
            ) if s.lot_id in lots_map else str(s.lot_id),
            "avg_weight": float(s.avg_weight) if s.avg_weight else None,
            "avg_length": float(s.avg_length) if s.avg_length else None,
            "condition_k": float(s.condition_k) if s.condition_k else None,
            "n_sampled": s.n_sampled,
        }
        for s in lot_stats_rows
    ]

    return {
        "session": {
            "id": session.id,
            "pond_id": session.pond_id,
            "pond_name": pond.name if pond else "",
            "pond_internal_id": pond.internal_id if pond else "",
            "registry_date": session.registry_date.isoformat() if session.registry_date else "",
            "notes": session.notes or "",
            "closed_at": session.closed_at.isoformat() if session.closed_at else None,
            "is_closed": session.closed_at is not None,
        },
        "records": records,
        "fish_options": fish_options,
        "lot_options": lot_options,
        "default_lot_id": lot_options[0]["id"] if len(lot_options) == 1 else None,
        "stats": {
            "count": len(records),
            "total_population": total_population,
            "pct_sampled": pct_sampled,
            "avg_weight_g": round(avg_weight, 1) if avg_weight else None,
            "estimated_biomass_kg": estimated_biomass,
            "pond_biomass_kg": float(pond.biomass) if pond and pond.biomass else None,
            "pond_avg_weight_g": float(pond.avg_weight) if pond and pond.avg_weight else None,
        },
        "lot_stats": lot_stats,
    }


@router.post("/ui/ponds/{pond_id}/sampling/new")
def ui_sampling_create(
    pond_id: int,
    registry_date: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Crea una nueva sesión de muestreo y redirige a ella."""
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    from datetime import date as date_type
    raw_date = (registry_date or "").strip()
    try:
        reg_date = date_type.fromisoformat(raw_date) if raw_date else date_type.today()
    except ValueError:
        reg_date = date_type.today()

    now = datetime.utcnow()
    session = SamplingSession(
        pond_id=pond_id,
        registry_date=reg_date,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    return RedirectResponse(
        url=f"/views/ui/sampling/{session.id}",
        status_code=303,
    )


@router.get("/ui/sampling/{session_id}", response_class=HTMLResponse)
def ui_sampling_detail(
    session_id: int,
    request: Request,
    status: Optional[str] = None,
    msg: Optional[str] = None,
    db: Session = Depends(get_db),
):
    session = db.query(SamplingSession).filter(SamplingSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Sesión de muestreo no encontrada")

    ctx = _get_sampling_session_context(session, db)
    ctx["request"] = request
    ctx["status"] = status
    ctx["msg"] = msg

    template = jinja_env.get_template("sampling_session.html")
    html = template.render(ctx)
    return HTMLResponse(content=html)


@router.post("/ui/sampling/{session_id}/close")
def ui_sampling_close(
    session_id: int,
    db: Session = Depends(get_db),
):
    """Cierra la sesión de muestreo: recalcula pond_lot_stats y biomasa del estanque."""
    session = db.query(SamplingSession).filter(SamplingSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if session.closed_at:
        return RedirectResponse(
            url=f"/views/ui/sampling/{session_id}?status=info&msg={quote_plus('La sesión ya estaba cerrada.')}",
            status_code=303,
        )

    records_count = db.query(SamplingRecord).filter(SamplingRecord.session_id == session_id).count()
    if records_count == 0:
        return RedirectResponse(
            url=f"/views/ui/sampling/{session_id}?status=error&msg={quote_plus('No hay registros. Agrega al menos uno antes de cerrar.')}",
            status_code=303,
        )

    try:
        _close_sampling_session(session, db)
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(
            url=f"/views/ui/sampling/{session_id}?status=error&msg={quote_plus('Error al cerrar la sesión. Intenta nuevamente.')}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/views/ui/sampling/{session_id}?status=ok&msg={quote_plus('Sesión cerrada y biomasa actualizada.')}",
        status_code=303,
    )


@router.post("/ui/sampling/{session_id}/records")
def ui_sampling_add_record(
    session_id: int,
    pit_tag: Optional[str] = Form(None),
    weight: Optional[str] = Form(None),
    length: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    redirect_base = f"/views/ui/sampling/{session_id}"

    def go(status_value: str, message: str):
        return RedirectResponse(
            url=f"{redirect_base}?status={quote_plus(status_value)}&msg={quote_plus(message)}",
            status_code=303,
        )

    session = db.query(SamplingSession).filter(SamplingSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    try:
        w = _parse_decimal_field(weight, "Peso")
        l = _parse_decimal_field(length, "Longitud")
    except ValueError as exc:
        return go("error", str(exc))

    if w is None and l is None:
        return go("error", "Debe ingresar al menos peso o longitud.")
    if w is not None and w <= 0:
        return go("error", "El peso debe ser mayor a 0.")
    if l is not None and l <= 0:
        return go("error", "La longitud debe ser mayor a 0.")

    # Resolver PIT tag → fish; lote siempre calculado
    fish_obj = None
    resolved_lot_id: Optional[int] = None
    pit_normalized = _normalize_pit_tag(pit_tag)
    if pit_normalized:
        fish_obj = db.query(Fish).filter(Fish.internal_id == pit_normalized).first()
        if not fish_obj:
            return go("error", f"PIT tag {pit_normalized} no encontrado en el sistema.")
        if fish_obj.state not in ("alive", "depuration"):
            return go("error", f"El pez {pit_normalized} no está activo.")
        # Verificar que el pez está en este estanque
        last_mv = (
            db.query(PondMovement)
            .filter(PondMovement.fish_id == fish_obj.id)
            .order_by(PondMovement.movement_time.desc(), PondMovement.id.desc())
            .first()
        )
        if not last_mv or last_mv.destiny_pond_id != session.pond_id:
            return go("error", f"El pez {pit_normalized} no está actualmente en este estanque.")
        resolved_lot_id = fish_obj.lot_id
    else:
        # Sin PIT: lote asignado por el estanque (saldo sin registrar)
        unregistered_balances = _get_unregistered_balances_by_lot(session.pond_id, db)
        active_lots = [lot_id for lot_id, qty in unregistered_balances.items() if qty > 0]
        if len(active_lots) == 1:
            resolved_lot_id = active_lots[0]
        elif len(active_lots) == 0:
            # Fallback: tomar lote de peces con PIT activos en el estanque
            tagged_fish = _get_current_tagged_fish_in_pond(session.pond_id, db)
            lot_ids = list({f.lot_id for f in tagged_fish if f.lot_id})
            if len(lot_ids) == 1:
                resolved_lot_id = lot_ids[0]
            elif len(lot_ids) > 1:
                return go("error", "El estanque tiene múltiples lotes. Ingrese un PIT tag para identificar el lote.")
            else:
                return go("error", "No se pudo determinar el lote del estanque.")
        else:
            return go("error", f"El estanque tiene saldo sin registrar de {len(active_lots)} lotes distintos. Ingrese un PIT tag para identificar el lote.")

    now = datetime.utcnow()
    try:
        record = SamplingRecord(
            session_id=session_id,
            fish_id=fish_obj.id if fish_obj else None,
            lot_id=resolved_lot_id,
            weight=w,
            length=l,
            notes=(notes or "").strip() or None,
            created_at=now,
            updated_at=now,
        )
        db.add(record)

        # Si tiene PIT → crear female_sampling para actualizar peso/longitud
        if fish_obj and (w is not None or l is not None):
            female_sample = FishSampling(
                fish_id=fish_obj.id,
                weight=w,
                length=l,
                registry_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(female_sample)

        db.commit()
        label = f"pez {pit_normalized}" if pit_normalized else "pez sin PIT"
        return go("ok", f"Registro guardado ({label}).")
    except Exception:
        db.rollback()
        return go("error", "No se pudo guardar el registro.")


@router.post("/ui/sampling/{session_id}/records/{record_id}/edit")
def ui_sampling_edit_record(
    session_id: int,
    record_id: int,
    weight: Optional[str] = Form(None),
    length: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    redirect_base = f"/views/ui/sampling/{session_id}"

    def go(status_value: str, message: str):
        return RedirectResponse(
            url=f"{redirect_base}?status={quote_plus(status_value)}&msg={quote_plus(message)}",
            status_code=303,
        )

    record = db.query(SamplingRecord).filter(
        SamplingRecord.id == record_id,
        SamplingRecord.session_id == session_id,
    ).first()
    if not record:
        return go("error", "Registro no encontrado.")

    try:
        w = _parse_decimal_field(weight, "Peso")
        l = _parse_decimal_field(length, "Longitud")
    except ValueError as exc:
        return go("error", str(exc))

    if w is None and l is None:
        return go("error", "Debe ingresar al menos peso o longitud.")
    if w is not None and w <= 0:
        return go("error", "El peso debe ser mayor a 0.")
    if l is not None and l <= 0:
        return go("error", "La longitud debe ser mayor a 0.")

    now = datetime.utcnow()
    try:
        record.weight = w
        record.length = l
        record.notes = (notes or "").strip() or None
        record.updated_at = now

        # Actualizar también el female_sampling más reciente del pez si tiene PIT
        if record.fish_id and (w is not None or l is not None):
            female_sample = FishSampling(
                fish_id=record.fish_id,
                weight=w,
                length=l,
                registry_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(female_sample)

        db.commit()
        return go("ok", "Registro actualizado.")
    except Exception:
        db.rollback()
        return go("error", "No se pudo actualizar el registro.")


# ---------------------------------------------------------------------------
# Tag detachment — pérdida de pit-tag
# ---------------------------------------------------------------------------

@router.get("/ui/ponds/{pond_id}/tag-detachment/new", response_class=HTMLResponse)
def ui_tag_detachment_new(
    pond_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    template = jinja_env.get_template("tag_detachment_form.html")
    html = template.render({"request": request, "pond": pond})
    return HTMLResponse(content=html)


@router.post("/ui/ponds/{pond_id}/tag-detachment")
def ui_tag_detachment_save(
    pond_id: int,
    request: Request,
    event_date: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    from datetime import datetime

    def go(s, m):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status={s}&msg={m}",
            status_code=303,
        )

    try:
        event_dt = datetime.fromisoformat(event_date)
    except ValueError:
        return go("error", "Fecha inválida.")

    try:
        # Registrar el evento
        event = TagDetachmentEvent(
            pond_id=pond_id,
            fish_id=None,
            event_date=event_dt,
            notes=notes.strip() or None,
            status="unidentified",
        )
        db.add(event)

        # Incrementar peces sin tag en el estanque
        pond.unregistered_count = (pond.unregistered_count or 0) + 1
        pond.n_fish_cached = (pond.tagged_count or 0) + pond.unregistered_count

        db.commit()

        # Actualizar cache completo del estanque
        _refresh_pond_runtime_cache(pond_id, db)

        return go("ok", "Pérdida de tag registrada. Pez sin tag sumado al estanque.")
    except Exception:
        db.rollback()
        return go("error", "No se pudo registrar la pérdida de tag.")


# ---------------------------------------------------------------------------
# Re-tag — asignar nuevo pit-tag a un pez sin tag (fase 2)
# ---------------------------------------------------------------------------

@router.get("/ui/ponds/{pond_id}/retag/{event_id}", response_class=HTMLResponse)
def ui_retag_form(
    pond_id: int,
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    event = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.id == event_id,
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status == "unidentified",
    ).first()
    if not pond or not event:
        raise HTTPException(status_code=404, detail="Evento no encontrado o ya procesado.")

    # Lotes disponibles en el estanque (para asignar lote al nuevo pez)
    unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)
    lot_ids = list(unregistered_balances.keys())
    lots = db.query(Lot).filter(Lot.id.in_(lot_ids)).all() if lot_ids else []
    lot_options = [
        {"id": lot.id, "label": lot.internal_id or lot.name, "qty": unregistered_balances[lot.id]}
        for lot in lots
    ]

    template = jinja_env.get_template("retag_form.html")
    html = template.render({
        "request": request,
        "pond": pond,
        "event": {"id": event.id, "event_date": event.event_date, "notes": event.notes},
        "lot_options": lot_options,
        "single_lot": lot_options[0] if len(lot_options) == 1 else None,
    })
    return HTMLResponse(content=html)


@router.post("/ui/ponds/{pond_id}/retag/{event_id}")
def ui_retag_save(
    pond_id: int,
    event_id: int,
    request: Request,
    internal_id: str = Form(...),
    lot_id: str = Form(...),
    sex: Optional[str] = Form(None),
    weight: Optional[str] = Form(None),
    confirm_reuse: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    event = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.id == event_id,
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status == "unidentified",
    ).first()

    def go(s, m):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status={s}&msg={quote_plus(m)}",
            status_code=303,
        )

    if not pond or not event:
        return go("error", "Evento no encontrado o ya procesado.")

    pit_tag = _normalize_pit_tag(internal_id)
    if not pit_tag:
        return go("error", "Debe ingresar un PIT tag.")

    tag_check = _check_pit_tag_reuse(pit_tag, db)
    if tag_check["status"] == "blocked":
        return go("error", tag_check["message"])
    if tag_check["status"] == "reuse":
        if confirm_reuse != "1":
            return go("warn", tag_check["message"])
        _archive_dead_fish_tag(pit_tag, tag_check["next_idx"], db)

    if not lot_id.strip().isdigit():
        return go("error", "Lote no válido.")
    selected_lot_id = int(lot_id.strip())

    unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)
    if unregistered_balances.get(selected_lot_id, 0) < 1:
        return go("error", "No hay saldo disponible del lote seleccionado.")

    try:
        new_weight = _parse_decimal_field(weight, "Peso")
    except ValueError as exc:
        return go("error", str(exc))

    target_sex = _normalize_sex_value(sex)
    now = datetime.utcnow()

    try:
        # Crear nuevo pez con el nuevo tag
        fish = Fish(
            internal_id=pit_tag,
            lot_id=selected_lot_id,
            sex=target_sex,
            state="depuration" if pond.depuration else "alive",
            registration_time=now,
            depuration_start_time=now if pond.depuration else None,
            created_at=now,
            updated_at=now,
        )
        db.add(fish)
        db.flush()

        # Sacar del pool de no-taggeados del estanque
        out_unregistered = PondMovement(
            fish_id=None,
            lot_id=selected_lot_id,
            source_pond_id=pond_id,
            destiny_pond_id=None,
            fish_quantity=1,
            movement_reason="registration",
            movement_time=now,
            created_at=now,
            updated_at=now,
        )
        db.add(out_unregistered)

        # Registrar entrada como pez taggeado
        in_registered = PondMovement(
            fish_id=fish.id,
            lot_id=selected_lot_id,
            source_pond_id=None,
            destiny_pond_id=pond_id,
            fish_quantity=1,
            movement_reason="registration",
            movement_time=now,
            created_at=now,
            updated_at=now,
        )
        db.add(in_registered)

        if new_weight is not None and new_weight > 0:
            sample = FishSampling(
                fish_id=fish.id,
                weight=new_weight,
                registry_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(sample)

        # Cerrar el evento de pérdida de tag
        event.retag_fish_id = fish.id
        event.status = "retagged"

        _adjust_biomass_on_movement(out_unregistered, db)
        _adjust_biomass_on_movement(in_registered, db)
        _refresh_pond_runtime_cache_many([pond_id], db)

        db.commit()
        return go("ok", f"PIT tag {pit_tag} asignado. Pez re-taggeado correctamente.")
    except Exception:
        db.rollback()
        return go("error", "No se pudo registrar el re-tag.")


# ---------------------------------------------------------------------------
# Reconciliación de tags perdidos al cierre del estanque (fase 3)
# ---------------------------------------------------------------------------

@router.get("/ui/ponds/{pond_id}/tag-reconciliation", response_class=HTMLResponse)
def ui_tag_reconciliation_form(
    pond_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    pending = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status == "unidentified",
    ).order_by(TagDetachmentEvent.event_date.asc()).all()

    if not pending:
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status=ok&msg=No+hay+eventos+pendientes+de+reconciliaci%C3%B3n.",
            status_code=303,
        )

    # Estado actual del estanque
    unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)
    unregistered_total = sum(unregistered_balances.values())
    tagged_total = len(_get_current_tagged_fish_in_pond(pond_id, db))

    events_data = [
        {
            "id": e.id,
            "event_date": e.event_date,
            "notes": e.notes or "",
        }
        for e in pending
    ]

    template = jinja_env.get_template("tag_reconciliation_form.html")
    html = template.render({
        "request": request,
        "pond": pond,
        "events": events_data,
        "unregistered_total": unregistered_total,
        "tagged_total": tagged_total,
    })
    return HTMLResponse(content=html)


@router.post("/ui/ponds/{pond_id}/tag-reconciliation")
async def ui_tag_reconciliation_save(
    pond_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    def go(s, m):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status={s}&msg={quote_plus(m)}",
            status_code=303,
        )

    form = await request.form()
    now = datetime.utcnow()

    pending = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status == "unidentified",
    ).all()

    if not pending:
        return go("ok", "No había eventos pendientes.")

    try:
        for event in pending:
            resolution = form.get(f"resolution_{event.id}", "left_unregistered")
            if resolution not in ("left_unregistered", "mortality", "other"):
                resolution = "left_unregistered"
            event.status = "written_off"
            event.resolution = resolution
            event.resolved_at = now

        db.commit()
        return go("ok", f"{len(pending)} evento(s) de tag perdido reconciliados y cerrados.")
    except Exception:
        db.rollback()
        return go("error", "No se pudo completar la reconciliación.")


# ---------------------------------------------------------------------------
# Vista general de lotes
# ---------------------------------------------------------------------------

def _build_lot_summaries(db: Session) -> list[dict]:
    """
    Construye el resumen de todos los lotes activos (con al menos 1 pez vivo).
    Usa queries agregados — sin N+1.
    """
    # Reparto de biomasa por lote usando la biomasa real de cada estanque.
    # Esto alinea la vista de lotes con la vista de estanques.
    lot_biomass_map, _pond_lot_biomass_map = _allocate_biomass_by_pond_lot(db)

    # 1. Peces tagged activos por lote
    tagged_rows = (
        db.query(Fish.lot_id, func.count(Fish.id).label("cnt"))
        .filter(Fish.state.in_(["alive", "depuration"]))
        .group_by(Fish.lot_id)
        .all()
    )
    tagged_by_lot = {int(r.lot_id): int(r.cnt) for r in tagged_rows if r.lot_id}

    # 2. Balance de peces sin registrar por lote (global, todos los estanques)
    in_rows = (
        db.query(PondMovement.lot_id, func.sum(PondMovement.fish_quantity).label("s"))
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.lot_id.isnot(None),
            PondMovement.destiny_pond_id.isnot(None),
        )
        .group_by(PondMovement.lot_id)
        .all()
    )
    out_rows = (
        db.query(PondMovement.lot_id, func.sum(PondMovement.fish_quantity).label("s"))
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.lot_id.isnot(None),
            PondMovement.source_pond_id.isnot(None),
        )
        .group_by(PondMovement.lot_id)
        .all()
    )
    unreg_in  = {int(r.lot_id): int(r.s or 0) for r in in_rows}
    unreg_out = {int(r.lot_id): int(r.s or 0) for r in out_rows}
    unreg_by_lot = {
        lot_id: unreg_in.get(lot_id, 0) - unreg_out.get(lot_id, 0)
        for lot_id in set(unreg_in) | set(unreg_out)
        if unreg_in.get(lot_id, 0) - unreg_out.get(lot_id, 0) > 0
    }

    # 3. Avg weight y condition K por lote desde pond_lot_stats
    stats_rows = (
        db.query(
            PondLotStats.lot_id,
            func.avg(PondLotStats.avg_weight).label("avg_w"),
            func.avg(PondLotStats.condition_k).label("avg_k"),
        )
        .filter(PondLotStats.lot_id.isnot(None))
        .group_by(PondLotStats.lot_id)
        .all()
    )
    stats_by_lot = {
        int(r.lot_id): {
            "avg_weight": float(r.avg_w) if r.avg_w else None,
            "avg_k": round(float(r.avg_k), 3) if r.avg_k else None,
        }
        for r in stats_rows
    }

    # 4. Lotes con actividad (tagged o sin registrar)
    active_lot_ids = set(tagged_by_lot) | set(unreg_by_lot)
    if not active_lot_ids:
        return []

    lots = (
        db.query(Lot)
        .filter(Lot.id.in_(active_lot_ids))
        .order_by(Lot.name)
        .all()
    )
    species_map = {s.id: s.name for s in db.query(Species).all()}

    summaries = []
    for lot in lots:
        tagged   = tagged_by_lot.get(lot.id, 0)
        unreg    = unreg_by_lot.get(lot.id, 0)
        n_fish   = tagged + unreg
        st       = stats_by_lot.get(lot.id, {})
        avg_w    = st.get("avg_weight")
        biomass_alloc = lot_biomass_map.get(lot.id)
        biomass = round(biomass_alloc, 1) if biomass_alloc and biomass_alloc > 0 else None

        summaries.append({
            "id": lot.id,
            "name": lot.name,
            "internal_id": lot.internal_id,
            "species": species_map.get(lot.species_id, "—"),
            "hatch_year": lot.hatch_year,
            "origin": lot.origin,
            "n_fish": n_fish,
            "tagged_count": tagged,
            "unregistered_count": unreg,
            "avg_weight": round(avg_w, 1) if avg_w else None,
            "biomass": biomass,
            "avg_k": st.get("avg_k"),
        })

    return summaries


def _allocate_biomass_by_pond_lot(db: Session) -> tuple[dict[int, float], dict[tuple[int, int], float]]:
    """
    Distribuye la biomasa real de cada estanque entre sus lotes activos.

    Regla de reparto por estanque:
    - peso relativo por lote = n_fish_lote_estanque * avg_weight_lote_estanque
    - fallback de avg_weight: pond.avg_weight; si no existe, 1.0
    - la suma de biomasa asignada por lotes en un estanque = pond.biomass_current
    """
    latest_id_subq = _latest_movement_id_per_fish_subq(db)

    # 1) Conteos tagged por (pond_id, lot_id)
    tagged_rows = (
        db.query(
            PondMovement.destiny_pond_id.label("pond_id"),
            Fish.lot_id.label("lot_id"),
            func.count(Fish.id).label("cnt"),
        )
        .join(Fish, Fish.id == PondMovement.fish_id)
        .join(latest_id_subq, latest_id_subq.c.max_id == PondMovement.id)
        .filter(
            PondMovement.destiny_pond_id.isnot(None),
            Fish.lot_id.isnot(None),
            Fish.state.in_(["alive", "depuration"]),
        )
        .group_by(PondMovement.destiny_pond_id, Fish.lot_id)
        .all()
    )

    tagged_map: dict[tuple[int, int], int] = {
        (int(r.pond_id), int(r.lot_id)): int(r.cnt)
        for r in tagged_rows
        if r.pond_id and r.lot_id
    }

    # 2) Balance sin tag por (pond_id, lot_id)
    unreg_in_rows = (
        db.query(
            PondMovement.destiny_pond_id.label("pond_id"),
            PondMovement.lot_id.label("lot_id"),
            func.sum(PondMovement.fish_quantity).label("s"),
        )
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.destiny_pond_id.isnot(None),
            PondMovement.lot_id.isnot(None),
        )
        .group_by(PondMovement.destiny_pond_id, PondMovement.lot_id)
        .all()
    )
    unreg_out_rows = (
        db.query(
            PondMovement.source_pond_id.label("pond_id"),
            PondMovement.lot_id.label("lot_id"),
            func.sum(PondMovement.fish_quantity).label("s"),
        )
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.source_pond_id.isnot(None),
            PondMovement.lot_id.isnot(None),
        )
        .group_by(PondMovement.source_pond_id, PondMovement.lot_id)
        .all()
    )

    unreg_in_map: dict[tuple[int, int], int] = {
        (int(r.pond_id), int(r.lot_id)): int(r.s or 0)
        for r in unreg_in_rows
        if r.pond_id and r.lot_id
    }
    unreg_out_map: dict[tuple[int, int], int] = {
        (int(r.pond_id), int(r.lot_id)): int(r.s or 0)
        for r in unreg_out_rows
        if r.pond_id and r.lot_id
    }

    # 3) Conteo total estimado por (pond_id, lot_id)
    pond_lot_count_map: dict[tuple[int, int], int] = {}
    all_keys = set(tagged_map) | set(unreg_in_map) | set(unreg_out_map)
    for key in all_keys:
        tagged = tagged_map.get(key, 0)
        unreg = max(0, unreg_in_map.get(key, 0) - unreg_out_map.get(key, 0))
        total = tagged + unreg
        if total > 0:
            pond_lot_count_map[key] = total

    # 4) avg_weight por (pond_id, lot_id)
    pond_lot_stats_rows = (
        db.query(PondLotStats.pond_id, PondLotStats.lot_id, PondLotStats.avg_weight)
        .filter(PondLotStats.lot_id.isnot(None), PondLotStats.pond_id.isnot(None))
        .all()
    )
    avg_weight_by_pond_lot: dict[tuple[int, int], float] = {
        (int(r.pond_id), int(r.lot_id)): float(r.avg_weight)
        for r in pond_lot_stats_rows
        if r.avg_weight
    }

    # 5) Biomasa real y avg_weight de estanque
    ponds = db.query(Pond.id, Pond.biomass_current, Pond.avg_weight).all()
    pond_biomass: dict[int, float] = {
        int(p.id): float(p.biomass_current)
        for p in ponds
        if p.biomass_current and float(p.biomass_current) > 0
    }
    pond_avg_weight: dict[int, float] = {
        int(p.id): float(p.avg_weight)
        for p in ponds
        if p.avg_weight and float(p.avg_weight) > 0
    }

    counts_by_pond: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for (pond_id, lot_id), n_fish in pond_lot_count_map.items():
        counts_by_pond[pond_id].append((lot_id, n_fish))

    pond_lot_biomass_map: dict[tuple[int, int], float] = {}
    lot_biomass_map: dict[int, float] = defaultdict(float)

    for pond_id, lot_rows in counts_by_pond.items():
        biomass = pond_biomass.get(pond_id)
        if not biomass or biomass <= 0:
            continue

        weighted: dict[int, float] = {}
        for lot_id, n_fish in lot_rows:
            lot_avg_w = avg_weight_by_pond_lot.get((pond_id, lot_id))
            if not lot_avg_w:
                lot_avg_w = pond_avg_weight.get(pond_id, 1.0)
            weighted[lot_id] = float(n_fish) * float(lot_avg_w)

        denom = sum(weighted.values())
        if denom <= 0:
            continue

        for lot_id, w in weighted.items():
            alloc = biomass * (w / denom)
            pond_lot_biomass_map[(pond_id, lot_id)] = alloc
            lot_biomass_map[lot_id] += alloc

    return dict(lot_biomass_map), pond_lot_biomass_map


@router.get("/ui/lots", response_class=HTMLResponse)
def ui_lots(
    request: Request,
    db: Session = Depends(get_db),
):
    summaries = _build_lot_summaries(db)

    total_fish    = sum(s["n_fish"] for s in summaries)
    total_biomass = sum(s["biomass"] for s in summaries if s["biomass"])
    k_vals        = [s["avg_k"] for s in summaries if s["avg_k"]]
    avg_k         = round(sum(k_vals) / len(k_vals), 3) if k_vals else None

    template = jinja_env.get_template("lots.html")
    html = template.render({
        "request": request,
        "lots": summaries,
        "stat_lots": len(summaries),
        "stat_fish": total_fish,
        "stat_biomass": round(total_biomass, 1) if total_biomass else None,
        "stat_k": avg_k,
    })
    return HTMLResponse(content=html)


@router.get("/ui/lots/{lot_id}", response_class=HTMLResponse)
def ui_lot_detail(
    lot_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    lot = db.query(Lot).filter(Lot.id == lot_id).first()
    if not lot:
        raise HTTPException(status_code=404, detail="Lot not found")

    species_map = {s.id: s.name for s in db.query(Species).all()}
    _lot_biomass_map, pond_lot_biomass_map = _allocate_biomass_by_pond_lot(db)

    # Estanques donde este lote está activo (usando cache de ponds)
    # active_lot_ids es un JSON array de ints
    active_ponds_db = db.query(Pond).all()
    lot_ponds = [p for p in active_ponds_db if lot_id in _as_int_list(p.active_lot_ids)]

    pond_types_map = {pt.id: pt.name for pt in db.query(PondType).all()}

    # Stats del lote en cada estanque
    pond_stats = (
        db.query(PondLotStats)
        .filter(PondLotStats.lot_id == lot_id)
        .all()
    )
    stats_by_pond = {s.pond_id: s for s in pond_stats}

    # Peces tagged del lote en cada estanque (un solo query)
    latest_id_subq = _latest_movement_id_per_fish_subq(db)
    fish_by_pond_rows = (
        db.query(PondMovement.destiny_pond_id, func.count(Fish.id).label("cnt"))
        .join(Fish, Fish.id == PondMovement.fish_id)
        .join(latest_id_subq, latest_id_subq.c.max_id == PondMovement.id)
        .filter(
            Fish.lot_id == lot_id,
            Fish.state.in_(["alive", "depuration"]),
            PondMovement.destiny_pond_id.isnot(None),
        )
        .group_by(PondMovement.destiny_pond_id)
        .all()
    )
    tagged_by_pond = {int(r.destiny_pond_id): int(r.cnt) for r in fish_by_pond_rows}

    # Peces sin registrar del lote por estanque
    unreg_in_rows = (
        db.query(PondMovement.destiny_pond_id, func.sum(PondMovement.fish_quantity).label("s"))
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.lot_id == lot_id,
            PondMovement.destiny_pond_id.isnot(None),
        )
        .group_by(PondMovement.destiny_pond_id)
        .all()
    )
    unreg_out_rows = (
        db.query(PondMovement.source_pond_id, func.sum(PondMovement.fish_quantity).label("s"))
        .filter(
            PondMovement.fish_id.is_(None),
            PondMovement.lot_id == lot_id,
            PondMovement.source_pond_id.isnot(None),
        )
        .group_by(PondMovement.source_pond_id)
        .all()
    )
    unreg_in_map  = {int(r.destiny_pond_id): int(r.s or 0) for r in unreg_in_rows}
    unreg_out_map = {int(r.source_pond_id):  int(r.s or 0) for r in unreg_out_rows}

    pond_rows = []
    for pond in sorted(lot_ponds, key=lambda p: p.name):
        tagged  = tagged_by_pond.get(pond.id, 0)
        unreg   = max(0, unreg_in_map.get(pond.id, 0) - unreg_out_map.get(pond.id, 0))
        n_fish  = tagged + unreg
        ps      = stats_by_pond.get(pond.id)
        avg_w   = float(ps.avg_weight) if ps and ps.avg_weight else None
        cond_k  = float(ps.condition_k) if ps and ps.condition_k else None
        alloc = pond_lot_biomass_map.get((pond.id, lot_id))
        biomass = round(alloc, 1) if alloc and alloc > 0 else None

        pond_rows.append({
            "pond_id":   pond.id,
            "pond_name": pond.name,
            "pond_type": pond_types_map.get(pond.pond_type_id, "—"),
            "depuration": pond.depuration,
            "n_fish":    n_fish,
            "tagged":    tagged,
            "unreg":     unreg,
            "avg_weight": round(avg_w, 1) if avg_w else None,
            "biomass":   biomass,
            "condition_k": round(cond_k, 3) if cond_k else None,
        })

    total_fish   = sum(r["n_fish"]  for r in pond_rows)
    total_biomass = sum(r["biomass"] for r in pond_rows if r["biomass"])
    k_vals = [r["condition_k"] for r in pond_rows if r["condition_k"]]
    avg_k  = round(sum(k_vals) / len(k_vals), 3) if k_vals else None
    avg_w_vals = [r["avg_weight"] for r in pond_rows if r["avg_weight"]]
    avg_w_global = round(sum(avg_w_vals) / len(avg_w_vals), 1) if avg_w_vals else None

    template = jinja_env.get_template("lot_detail.html")
    html = template.render({
        "request": request,
        "lot": {
            "id": lot.id,
            "name": lot.name,
            "internal_id": lot.internal_id,
            "species": species_map.get(lot.species_id, "—"),
            "hatch_year": lot.hatch_year,
            "origin": lot.origin,
        },
        "pond_rows": pond_rows,
        "stat_fish": total_fish,
        "stat_biomass": round(total_biomass, 1) if total_biomass else None,
        "stat_k": avg_k,
        "stat_avg_weight": avg_w_global,
    })
    return HTMLResponse(content=html)


# ── Vista Faena ─────────────────────────────────────────────────────────────

@router.get("/ui/faena", response_class=HTMLResponse)
def ui_faena(request: Request, db: Session = Depends(get_db)):
    """Lista todos los peces en estado 'faena' con sus datos completos para despacho."""

    # Peces en estado faena
    fish_in_faena = (
        db.query(Fish)
        .filter(Fish.state == "faena")
        .order_by(Fish.lot_id.asc(), Fish.id.asc())
        .all()
    )

    fish_ids = [f.id for f in fish_in_faena]

    # Último muestreo por pez (peso, diámetro, estado desarrollo)
    if fish_ids:
        latest_sampling_subq = (
            db.query(
                FishSampling.fish_id,
                func.max(func.coalesce(FishSampling.registry_time, FishSampling.created_at)).label("max_sample_time"),
            )
            .filter(FishSampling.fish_id.in_(fish_ids))
            .group_by(FishSampling.fish_id)
            .subquery()
        )
        latest_samples = (
            db.query(FishSampling)
            .join(
                latest_sampling_subq,
                (latest_sampling_subq.c.fish_id == FishSampling.fish_id) &
                (latest_sampling_subq.c.max_sample_time == func.coalesce(FishSampling.registry_time, FishSampling.created_at))
            )
            .all()
        )
        samples_map = {s.fish_id: s for s in latest_samples}
    else:
        samples_map = {}

    # Último movimiento por pez (estanque origen + fecha de despacho)
    if fish_ids:
        faena_movements = (
            db.query(PondMovement)
            .filter(
                PondMovement.fish_id.in_(fish_ids),
                PondMovement.movement_reason == "faena",
                PondMovement.destiny_pond_id.is_(None),
            )
            .order_by(PondMovement.fish_id.asc(), PondMovement.movement_time.desc())
            .all()
        )
        # Último movimiento de faena por pez
        faena_mov_map: dict[int, PondMovement] = {}
        for mv in faena_movements:
            if mv.fish_id not in faena_mov_map:
                faena_mov_map[mv.fish_id] = mv
    else:
        faena_mov_map = {}

    # Última transición alive→depuración por pez (sin resetear si se mueve entre estanques de depuración)
    # Criterio: último movimiento donde destiny_pond.depuration=True y source_pond.depuration=False (o sin origen)
    dep_entry_map: dict[int, PondMovement] = {}
    if fish_ids:
        SourcePond = aliased(Pond)
        DestPond = aliased(Pond)
        dep_transition_rows = (
            db.query(PondMovement, PondMovement.movement_time)
            .join(DestPond, DestPond.id == PondMovement.destiny_pond_id)
            .outerjoin(SourcePond, SourcePond.id == PondMovement.source_pond_id)
            .filter(
                PondMovement.fish_id.in_(fish_ids),
                DestPond.depuration.is_(True),
                or_(
                    PondMovement.source_pond_id.is_(None),
                    SourcePond.depuration.is_(False),
                ),
            )
            .order_by(PondMovement.fish_id.asc(), PondMovement.movement_time.desc())
            .all()
        )
        for row in dep_transition_rows:
            mv = row[0]
            if mv.fish_id not in dep_entry_map:
                dep_entry_map[mv.fish_id] = mv

    # Estanques de origen
    source_pond_ids = {mv.source_pond_id for mv in faena_mov_map.values() if mv.source_pond_id}
    ponds_map: dict[int, Pond] = {}
    if source_pond_ids:
        ponds_map = {p.id: p for p in db.query(Pond).filter(Pond.id.in_(source_pond_ids)).all()}

    # Lotes
    lot_ids = {f.lot_id for f in fish_in_faena if f.lot_id}
    lots_map: dict[int, Lot] = {}
    if lot_ids:
        lots_map = {lot.id: lot for lot in db.query(Lot).filter(Lot.id.in_(lot_ids)).all()}

    # Depuración: días en depuración al momento del despacho
    # Calculamos desde fish.depuration_start_time (ya registrado) hasta fecha del movimiento faena
    fish_rows = []
    for fish in fish_in_faena:
        sample = samples_map.get(fish.id)
        mov = faena_mov_map.get(fish.id)
        lot = lots_map.get(fish.lot_id)
        source_pond = ponds_map.get(mov.source_pond_id) if mov and mov.source_pond_id else None

        sex_norm = (fish.sex or "").strip().lower()
        is_female = sex_norm in ["f", "female", "hembra"]
        sex_label = "Hembra" if is_female else ("Macho" if sex_norm in ["m", "male", "macho"] else "N/D")

        # Peso en gramos y kg (el muestreo final guardado al enviar a faena)
        weight_g = float(sample.weight) if sample and sample.weight is not None else None
        weight_kg = round(weight_g / 1000.0, 3) if weight_g is not None else None

        # Diámetro de ovas (solo hembras)
        caviar_diameter = (
            float(sample.diameter) if is_female and sample and sample.diameter is not None else None
        )

        # Estado de desarrollo
        dev_state = (
            str(sample.development_state).strip().upper()
            if sample and sample.development_state else None
        )

        # Días en depuración al momento del despacho
        # Regla: desde el último movimiento con reason="depuration" hasta la fecha de faena
        dep_days = None
        dep_entry = dep_entry_map.get(fish.id)
        if dep_entry and dep_entry.movement_time and mov and mov.movement_time:
            dep_days = (mov.movement_time.date() - dep_entry.movement_time.date()).days
            if dep_days < 0:
                dep_days = 0

        # Fecha de despacho
        dispatched_at = mov.movement_time if mov else None

        fish_rows.append({
            "fish_id": fish.id,
            "internal_id": fish.internal_id or "N/D",
            "lot": (lot.internal_id or lot.name) if lot else "N/D",
            "lot_id": fish.lot_id,
            "sex": sex_label,
            "sex_value": "f" if is_female else ("m" if sex_norm in ["m", "male", "macho"] else ""),
            "weight_g": weight_g,
            "weight_kg": weight_kg,
            "caviar_diameter": caviar_diameter,
            "dev_state": dev_state,
            "depuration_days": dep_days,
            "source_pond": source_pond.name if source_pond else "N/D",
            "dispatched_at": dispatched_at,
        })

    # Estadísticas globales
    total_fish = len(fish_rows)
    total_biomass_kg = sum(r["weight_kg"] for r in fish_rows if r["weight_kg"] is not None)
    females_count = sum(1 for r in fish_rows if r["sex_value"] == "f")
    state4_count = sum(1 for r in fish_rows if r["sex_value"] == "f" and r["dev_state"] == "4")
    caviar_est_kg = round(
        sum(r["weight_kg"] for r in fish_rows if r["sex_value"] == "f" and r["dev_state"] == "4" and r["weight_kg"]) * 0.125,
        1
    )

    # Agrupar por lote para el resumen
    by_lot: dict = {}
    for r in fish_rows:
        lid = r["lot_id"] or 0
        if lid not in by_lot:
            by_lot[lid] = {"label": r["lot"], "count": 0, "biomass_kg": 0.0}
        by_lot[lid]["count"] += 1
        by_lot[lid]["biomass_kg"] += r["weight_kg"] or 0.0
    lot_summary = [
        {"lot": v["label"], "count": v["count"], "biomass_kg": round(v["biomass_kg"], 1)}
        for v in by_lot.values()
    ]

    template = jinja_env.get_template("faena.html")
    html = template.render({
        "request": request,
        "fish_rows": fish_rows,
        "total_fish": total_fish,
        "total_biomass_kg": round(total_biomass_kg, 1),
        "females_count": females_count,
        "state4_count": state4_count,
        "caviar_est_kg": caviar_est_kg,
        "lot_summary": lot_summary,
    })
    return HTMLResponse(content=html)

