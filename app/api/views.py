from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from collections import defaultdict
from urllib.parse import quote_plus
import bisect
import re
import json
from zoneinfo import ZoneInfo
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
from app.models.biomass_checkpoint import BiomassCheckpoint
from app.models.tag_detachment_events import TagDetachmentEvent
from app.models.fish_drug_uses import FishDrugUse
from app.models.species import Species
from app.models.sanitary_reports import SanitaryReport
from app.models.cultivation_declarations import CultivationDeclaration
from app.models.cultivation_declaration_items import CultivationDeclarationItem
from app.schemas.views import CultivationUnitWithPonds, PondSummary, LotSummary
from app.models.feed import (FeedMonthlyConsumptionLegacy, FeedExecutionEvent,
                             FeedType, FeedReceiptHeader, FeedReceiptLine)
from app.services.planta_yield import get_caviar_yield_factor
from app.services import ovulation_cycle as _ovc
from app.services import reproduccion as _repro
from app.services import oocyte_growth as _og

# Estados de desarrollo maduros: solo estas hembras tienen ova lista y se les
# estima caviar. Las inmaduras muestran biomasa pero sin estimado.
CAVIAR_MATURE_STATES = {"3", "4"}

router = APIRouter(prefix="/views", tags=["views"])
template_dir = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)))

STALE_WEIGHT_DAYS = 90

# ---------------------------------------------------------------------------
# Reporte de alimentación mensual — constantes de normalización
# ---------------------------------------------------------------------------
FEED_TYPE_DISPLAY_ORDER = [
    ("infa_0.2mm",       "Infa 0.2"),
    ("infa_0.4mm",       "Infa 0.4"),
    ("infa_combinado",   "Infa 0.2/0.4"),
    ("thalassa_0.5mm",   "Thalassa 0.5"),
    ("thalassa_0.9mm",   "Thalassa 0.9"),
    ("thalassa_1.3mm",   "Thalassa 1.3"),
    ("progress_3mm",     "Progress 3mm"),
    ("progress_4.5mm",   "Progress 4.5"),
    ("progress_6mm",     "Progress 6mm"),
    ("progress_8mm",     "Progress 8mm"),
    ("sturgeon_rep_6mm", "Repr. 6mm"),
    ("reproductor_8mm",  "Repr. 8mm"),
    ("ewos_psz_4mm",     "Ewos PSZ 4"),
    ("ewos_psz_6mm",     "Ewos PSZ 6"),
    ("ewos_breed_9.5mm", "Ewos Breed 9.5"),
]
FEED_TYPE_DISPLAY = {k: v for k, v in FEED_TYPE_DISPLAY_ORDER}

OPERATIONAL_TO_FEED_KEY = {
    "Infa 0.2":           "infa_0.2mm",
    "Infa 0.4":           "infa_0.4mm",
    "Thalassa 0.5/1.0":   "thalassa_0.5mm",
    "Thalassa 0.9/1.6":   "thalassa_0.9mm",
    "Thalassa 1.3/2.0":   "thalassa_1.3mm",
    "Progress EX 3.0":    "progress_3mm",
    "Progress EX 4.5":    "progress_4.5mm",
    "Progress EX 6.0":    "progress_6mm",
    "Progress EX 8.0":    "progress_8mm",
    "Reproductor EX 6.0": "sturgeon_rep_6mm",
    "Reproductor EX 8.0": "reproductor_8mm",
}

CU_DISPLAY_ORDER = [
    "Sur Oriente", "Sur Poniente", "Nor-Oriente",
    "Nor-Central", "Nor-Poniente", "Central",
    "Jaula", "Hatchery: Alevinaje", "Hatchery Sala 2", "Hatchery: Incubacion",
]
RECENT_LOT_WEIGHT_LOOKBACK_DAYS = 365
MIN_RECENT_LOT_SAMPLES_FOR_FLOOR = 10
APP_LOCAL_TZ = ZoneInfo("America/Santiago")


def _to_local_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(APP_LOCAL_TZ)


def _format_local_datetime(value: Optional[datetime]) -> Optional[str]:
    local_dt = _to_local_datetime(value)
    return local_dt.strftime("%d/%m/%Y %H:%M") if local_dt else None

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


def _get_current_marked_lot_ids_in_pond(pond_id: int, db: Session) -> list[int]:
    """Lotes de peces marcados cuyo último movimiento los deja en esta laguna."""
    latest_id_subq = _latest_movement_id_per_fish_subq(db)
    rows = (
        db.query(Fish.lot_id)
        .join(PondMovement, PondMovement.fish_id == Fish.id)
        .join(latest_id_subq, latest_id_subq.c.max_id == PondMovement.id)
        .filter(
            PondMovement.destiny_pond_id == pond_id,
            PondMovement.fish_id.isnot(None),
            Fish.lot_id.isnot(None),
        )
        .distinct()
        .all()
    )
    return sorted(int(row[0]) for row in rows if row and row[0] is not None)


def _get_pending_drug_log_fish_query(db: Session):
    """Peces con sufijo _R, _RNN o _RNN_RNN pero sin registros en fish_drug_uses."""
    return (
        db.query(Fish)
        .outerjoin(FishDrugUse, FishDrugUse.fish_id == Fish.id)
        .filter(
            Fish.internal_id.isnot(None),
            func.upper(func.trim(Fish.internal_id)).op("~")("(_R[0-9]{1,2}){1,2}$|_R$"),
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


def _as_int_dict(raw_value) -> dict[int, int]:
    if not raw_value or not isinstance(raw_value, dict):
        return {}

    out: dict[int, int] = {}
    for key, value in raw_value.items():
        try:
            lot_id = int(key)
            qty = int(value)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            out[lot_id] = qty
    return out


def _get_current_tagged_count_and_lot_ids(pond_id: int, db: Session) -> tuple[int, set[int]]:
    latest_id_subq = _latest_movement_id_per_fish_subq(db)
    rows = (
        db.query(Fish.id, Fish.lot_id)
        .join(PondMovement, PondMovement.fish_id == Fish.id)
        .join(latest_id_subq, latest_id_subq.c.max_id == PondMovement.id)
        .filter(
            PondMovement.destiny_pond_id == pond_id,
            Fish.state.in_(["alive", "depuration"]),
        )
        .all()
    )
    lot_ids = {int(row[1]) for row in rows if row and row[1] is not None}
    return len(rows), lot_ids


def _get_pond_available_lot_ids(pond_id: int, db: Session) -> list[int]:
    """Lotes disponibles de la laguna, combinando estado actual e historial."""
    lot_ids: set[int] = set()

    # 1) Lotes con peces sin PIT actualmente en el estanque.
    lot_ids.update(_get_unregistered_balances_by_lot(pond_id, db).keys())

    # 2) Lotes de peces con PIT actualmente en el estanque (vía helper con estado activo).
    lot_ids.update(
        int(f.lot_id)
        for f in _get_current_tagged_fish_in_pond(pond_id, db)
        if f.lot_id is not None
    )

    # 2b) Fallback robusto sin depender del estado del pez.
    lot_ids.update(_get_current_marked_lot_ids_in_pond(pond_id, db))

    # 3) Cache runtime del estanque (cuando está disponible).
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if pond and pond.active_lot_ids:
        for item in _as_int_list(pond.active_lot_ids):
            lot_ids.add(int(item))

    # 4) Stats de lote en estanque (historial de muestreo/biomasa).
    stats_rows = (
        db.query(PondLotStats.lot_id)
        .filter(PondLotStats.pond_id == pond_id, PondLotStats.lot_id.isnot(None))
        .distinct()
        .all()
    )
    lot_ids.update(int(row[0]) for row in stats_rows if row and row[0] is not None)

    # 5) Historial de movimientos en la laguna.
    # Siempre se incorpora para no perder lotes válidos en casos de estado/caché desalineado.
    movement_rows = (
        db.query(PondMovement.lot_id)
        .filter(
            PondMovement.lot_id.isnot(None),
            or_(
                PondMovement.source_pond_id == pond_id,
                PondMovement.destiny_pond_id == pond_id,
            ),
        )
        .distinct()
        .all()
    )
    lot_ids.update(int(row[0]) for row in movement_rows if row and row[0] is not None)

    return sorted(lot_ids)


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

    tagged_count, tagged_lot_ids = _get_current_tagged_count_and_lot_ids(pond_id, db)

    unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)
    unregistered_count = sum(unregistered_balances.values())
    unregistered_lot_ids = {int(lot_id) for lot_id in unregistered_balances.keys()}

    active_lot_ids = sorted(tagged_lot_ids | unregistered_lot_ids)
    unregistered_lot_ids_sorted = sorted(unregistered_lot_ids)

    pond.tagged_count = tagged_count
    pond.unregistered_count = unregistered_count

    # Recuento usando fórmula de movimientos: IN - OUT - RETAGGED_UNRESOLVED
    # Esto refleja la realidad del estanque (evita duplicación de peces re-taggeados)
    try:
        retagged_count = db.query(func.count(TagDetachmentEvent.id)).filter(
            TagDetachmentEvent.pond_id == pond_id,
            TagDetachmentEvent.status == "retagged",
            TagDetachmentEvent.resolved_at.is_(None),
        ).scalar() or 0
        pond.n_fish_cached = tagged_count + unregistered_count - retagged_count
    except Exception:
        # Fallback a método antiguo si hay error
        pond.n_fish_cached = tagged_count + unregistered_count

    pond.active_lots_count = len(active_lot_ids)
    pond.active_lot_ids = active_lot_ids
    pond.unregistered_lot_ids = unregistered_lot_ids_sorted
    pond.unregistered_balances_by_lot = {str(k): int(v) for k, v in unregistered_balances.items()}
    pond.unregistered_lot_conflict = len(unregistered_lot_ids_sorted) > 1
    pond.runtime_cache_updated_at = datetime.utcnow()


def _refresh_pond_runtime_cache_many(pond_ids, db: Session) -> None:
    unique_ids = sorted({int(pid) for pid in pond_ids if pid})
    for pid in unique_ids:
        _refresh_pond_runtime_cache(pid, db)


def rebuild_all_pond_runtime_cache(db: Session) -> int:
    """Recalcula y persiste el cache operativo de todos los estanques.

    Fuente de verdad única para el script `rebuild_pond_runtime_cache.py` y el
    job agendado. Hace commit y devuelve la cantidad de estanques procesados.
    """
    ponds = db.query(Pond).order_by(Pond.id).all()
    for pond in ponds:
        _refresh_pond_runtime_cache(pond.id, db)
    db.commit()
    return len(ponds)


def _build_cached_pond_rows(
    ponds: List[Pond],
    db: Session,
    pond_types_map: Optional[dict[int, str]] = None,
    include_unregistered_flags: bool = False,
) -> list[dict]:
    all_lot_ids: set[int] = set()
    for pond in ponds:
        all_lot_ids.update(_as_int_list(pond.active_lot_ids))
        if pond.lot_id:
            all_lot_ids.add(int(pond.lot_id))  # lote asignado sin recuento (eclosión)

    lots_map = {
        lot.id: lot
        for lot in db.query(Lot).filter(Lot.id.in_(all_lot_ids)).all()
    } if all_lot_ids else {}

    # Biomasa proyectada desde checkpoints (agrupa por estanque)
    from datetime import date as _date
    _proj, _ = _project_biomass_from_checkpoint(_date.today(), db)
    pond_biomass_proj: dict[int, float] = defaultdict(float)
    for (_pid, _lid), _bm in _proj.items():
        pond_biomass_proj[_pid] += _bm

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

        # Lote asignado por eclosión, aún sin primer recuento (§9.6): se muestra
        # como pendiente. No está en active_lot_ids porque no hay movimientos con
        # cantidad; se toma de pond.lot_id y se de-duplica contra los ya listados.
        assigned_lot_id = int(pond.lot_id) if pond.lot_id else None
        if assigned_lot_id and assigned_lot_id not in active_lot_ids:
            assigned_lot = lots_map.get(assigned_lot_id)
            if assigned_lot:
                lot_item = {
                    "id": int(assigned_lot.id),
                    "name": assigned_lot.name,
                    "internal_id": assigned_lot.internal_id,
                    "is_pending_count": True,
                }
                if include_unregistered_flags:
                    lot_item["is_unregistered_lot"] = False
                active_lots.append(lot_item)

        biomass_proj = pond_biomass_proj.get(pond.id) or None
        row = {
            "id": pond.id,
            "name": pond.name,
            "internal_id": pond.internal_id,
            "code": pond.code,
            "depuration": pond.depuration,
            "state": "depuration" if pond.depuration else pond.state,
            "volume": pond.volume,
            "n_fish": int(pond.n_fish_cached or 0),
            "biomass": round(biomass_proj, 1) if biomass_proj else None,
            "density": round(biomass_proj / pond.volume, 2) if (biomass_proj and pond.volume and pond.volume > 0) else None,
            "avg_weight": float(pond.avg_weight) if pond.avg_weight else None,
            "active_lots": active_lots,
            "unregistered_lot_conflict": bool(pond.unregistered_lot_conflict),
            "biomass_measured": float(pond.biomass_measured) if pond.biomass_measured else None,
        }
        if pond_types_map is not None:
            row["pond_type_name"] = pond_types_map.get(pond.pond_type_id, "—")

        rows.append(row)

    return rows


def _parse_month_for_reports(month_value: Optional[str]) -> tuple[str, datetime, datetime, datetime]:
    raw = (month_value or "").strip()
    now = datetime.utcnow()
    if not raw:
        start = datetime(now.year, now.month, 1)
    else:
        try:
            start = datetime.strptime(raw, "%Y-%m")
            start = datetime(start.year, start.month, 1)
        except ValueError:
            start = datetime(now.year, now.month, 1)

    if start.month == 12:
        next_month = datetime(start.year + 1, 1, 1)
    else:
        next_month = datetime(start.year, start.month + 1, 1)
    month_end = next_month - timedelta(seconds=1)
    return start.strftime("%Y-%m"), start, next_month, month_end


def _sum_center_lot_balance_until(cutoff_exclusive: datetime, db: Session) -> dict[int, int]:
    incoming_rows = (
        db.query(PondMovement.lot_id, func.coalesce(func.sum(PondMovement.fish_quantity), 0))
        .filter(
            PondMovement.lot_id.isnot(None),
            PondMovement.movement_time < cutoff_exclusive,
            PondMovement.destiny_pond_id.isnot(None),
            PondMovement.source_pond_id.is_(None),
        )
        .group_by(PondMovement.lot_id)
        .all()
    )
    outgoing_rows = (
        db.query(PondMovement.lot_id, func.coalesce(func.sum(PondMovement.fish_quantity), 0))
        .filter(
            PondMovement.lot_id.isnot(None),
            PondMovement.movement_time < cutoff_exclusive,
            PondMovement.source_pond_id.isnot(None),
            PondMovement.destiny_pond_id.is_(None),
        )
        .group_by(PondMovement.lot_id)
        .all()
    )

    balances: dict[int, int] = {}
    for lot_id, qty in incoming_rows:
        lid = int(lot_id)
        balances[lid] = balances.get(lid, 0) + int(qty or 0)
    for lot_id, qty in outgoing_rows:
        lid = int(lot_id)
        balances[lid] = balances.get(lid, 0) - int(qty or 0)

    return {lot_id: qty for lot_id, qty in balances.items() if qty != 0}


def _sum_center_lot_reason_between(
    start_inclusive: datetime,
    end_exclusive: datetime,
    reason: str,
    db: Session,
) -> dict[int, int]:
    rows = (
        db.query(PondMovement.lot_id, func.coalesce(func.sum(PondMovement.fish_quantity), 0))
        .filter(
            PondMovement.lot_id.isnot(None),
            PondMovement.movement_time >= start_inclusive,
            PondMovement.movement_time < end_exclusive,
            PondMovement.source_pond_id.isnot(None),
            PondMovement.destiny_pond_id.is_(None),
            PondMovement.movement_reason == reason,
        )
        .group_by(PondMovement.lot_id)
        .all()
    )
    return {int(lot_id): int(qty or 0) for lot_id, qty in rows if int(qty or 0) != 0}


def _build_lot_rolling_avg(
    lot_ids: set[int],
    snapshot_dt: "datetime | date",
    db: Session,
    days: int = 90,
    lookahead_days: int = 45,
) -> dict[int, float]:
    """
    Promedio de peso por lote desde sampling_records de sesiones cerradas.

    Ventana: [snap - days, snap + lookahead_days].

    Lógica de prioridad para cada lote:
    1. Datos retroactivos RECIENTES (sesión más reciente <= snap y dentro de 30 días antes):
       la medición más próxima antes del corte es representativa → usarla.
    2. Lookahead cercano (sesión > snap, dentro de 30 días después):
       si los datos retroactivos son >30 días viejos hay que usar el muestreo posterior
       más próximo al corte — biológicamente es el mejor estimado del peso en esa fecha.
    3. Datos retroactivos aunque sean viejos (fallback de la ventana completa).
    4. Lookahead lejano (>30 días después del corte).
    """
    if not lot_ids:
        return {}
    from datetime import timedelta
    snap = snapshot_dt.date() if isinstance(snapshot_dt, datetime) else snapshot_dt
    cutoff = snap - timedelta(days=days)
    forward = snap + timedelta(days=lookahead_days)
    snap_minus_30 = snap - timedelta(days=30)
    snap_plus_30  = snap + timedelta(days=30)

    rows = db.execute(
        text("""
            SELECT sr.lot_id,
                   -- Franja 1: los 30 días inmediatamente anteriores al corte (más representativos)
                   AVG(sr.weight) FILTER (WHERE ss.registry_date > :snap_m30
                                            AND ss.registry_date <= :snap)             AS avg_w_back_30d,
                   -- Franja 2: lookahead cercano (≤30 días después del corte)
                   AVG(sr.weight) FILTER (WHERE ss.registry_date > :snap
                                            AND ss.registry_date <= :snap_p30)         AS avg_w_fwd_close,
                   -- Franja 3: datos retroactivos más viejos (31-90 días)
                   AVG(sr.weight) FILTER (WHERE ss.registry_date <= :snap_m30)         AS avg_w_back_old,
                   -- Franja 4: lookahead lejano (último recurso)
                   AVG(sr.weight) FILTER (WHERE ss.registry_date > :snap_p30)          AS avg_w_fwd_far
            FROM sampling_records sr
            JOIN sampling_sessions ss ON ss.id = sr.session_id
            WHERE ss.registry_date BETWEEN :cutoff AND :forward
              AND ss.closed_at IS NOT NULL
              AND sr.weight IS NOT NULL
              AND sr.lot_id = ANY(:lids)
            GROUP BY sr.lot_id
        """),
        {
            "cutoff": cutoff, "snap": snap, "forward": forward,
            "snap_m30": snap_minus_30, "snap_p30": snap_plus_30,
            "lids": list(lot_ids),
        },
    ).fetchall()

    result: dict[int, float] = {}
    for r in rows:
        lid = int(r[0])
        back_30d  = float(r[1]) if r[1] else None
        fwd_close = float(r[2]) if r[2] else None
        back_old  = float(r[3]) if r[3] else None
        fwd_far   = float(r[4]) if r[4] else None
        # Jerarquía: más cercano al corte primero; preferencia por retroactivo
        result[lid] = (back_30d or fwd_close or back_old or fwd_far)  # type: ignore[assignment]
    result = {k: v for k, v in result.items() if v is not None}
    return result


def _build_lot_weight_snapshot(lot_ids: set[int], snapshot_dt: datetime, db: Session) -> dict[int, float]:
    if not lot_ids:
        return {}

    snapshot_date = snapshot_dt.date() if isinstance(snapshot_dt, datetime) else snapshot_dt

    rows = (
        db.query(PondLotStats.lot_id, PondLotStats.avg_weight, PondLotStats.sampled_at, PondLotStats.updated_at, PondLotStats.id)
        .filter(PondLotStats.lot_id.in_(lot_ids), PondLotStats.avg_weight.isnot(None))
        .order_by(
            PondLotStats.lot_id.asc(),
            PondLotStats.sampled_at.desc().nullslast(),   # primero por fecha real del muestreo
            PondLotStats.updated_at.desc().nullslast(),
            PondLotStats.id.desc(),
        )
        .all()
    )

    fallback_map: dict[int, float] = {}
    before_map: dict[int, float] = {}
    for lot_id, avg_weight, sampled_at, updated_at, _row_id in rows:
        lid = int(lot_id)
        w = float(avg_weight)
        # effective_date: usa sampled_at (fecha real del muestreo) con fallback a updated_at
        effective_date = sampled_at if sampled_at is not None else (updated_at.date() if updated_at else None)
        if lid not in fallback_map:
            fallback_map[lid] = w
        if lid not in before_map and effective_date and effective_date <= snapshot_date:
            before_map[lid] = w

    # Rolling avg de sampling_records en los 90 días anteriores al snapshot.
    # Tiene prioridad sobre pond_lot_stats porque agrega más mediciones y evita
    # sesgos de muestras únicas de un solo estanque.
    rolling_avg = _build_lot_rolling_avg(lot_ids, snapshot_dt, db)

    out: dict[int, float] = {}
    for lid in lot_ids:
        if lid in rolling_avg:
            out[lid] = rolling_avg[lid]
        elif lid in before_map:
            out[lid] = before_map[lid]
        else:
            out[lid] = fallback_map.get(lid, 0.0)
    return out


def _sum_pond_lot_balance_until(cutoff_exclusive: datetime, db: Session) -> dict[tuple[int, int], int]:
    incoming_rows = (
        db.query(
            PondMovement.destiny_pond_id,
            PondMovement.lot_id,
            func.coalesce(func.sum(PondMovement.fish_quantity), 0),
        )
        .filter(
            PondMovement.destiny_pond_id.isnot(None),
            PondMovement.lot_id.isnot(None),
            PondMovement.movement_time < cutoff_exclusive,
        )
        .group_by(PondMovement.destiny_pond_id, PondMovement.lot_id)
        .all()
    )
    outgoing_rows = (
        db.query(
            PondMovement.source_pond_id,
            PondMovement.lot_id,
            func.coalesce(func.sum(PondMovement.fish_quantity), 0),
        )
        .filter(
            PondMovement.source_pond_id.isnot(None),
            PondMovement.lot_id.isnot(None),
            PondMovement.movement_time < cutoff_exclusive,
        )
        .group_by(PondMovement.source_pond_id, PondMovement.lot_id)
        .all()
    )

    balances: dict[tuple[int, int], int] = defaultdict(int)
    for pond_id, lot_id, qty in incoming_rows:
        balances[(int(pond_id), int(lot_id))] += int(qty or 0)
    for pond_id, lot_id, qty in outgoing_rows:
        balances[(int(pond_id), int(lot_id))] -= int(qty or 0)

    return {k: v for k, v in balances.items() if v != 0}


def _sum_pond_lot_between(
    start_inclusive: datetime,
    end_exclusive: datetime,
    mode: str,
    db: Session,
) -> dict[tuple[int, int], int]:
    if mode in ("mortality", "faena"):
        rows = (
            db.query(
                PondMovement.source_pond_id,
                PondMovement.lot_id,
                func.coalesce(func.sum(PondMovement.fish_quantity), 0),
            )
            .filter(
                PondMovement.source_pond_id.isnot(None),
                PondMovement.destiny_pond_id.is_(None),
                PondMovement.lot_id.isnot(None),
                PondMovement.movement_time >= start_inclusive,
                PondMovement.movement_time < end_exclusive,
                PondMovement.movement_reason == mode,
            )
            .group_by(PondMovement.source_pond_id, PondMovement.lot_id)
            .all()
        )
        return {
            (int(pond_id), int(lot_id)): int(qty or 0)
            for pond_id, lot_id, qty in rows
            if int(qty or 0) != 0
        }

    if mode == "salidas":
        rows = (
            db.query(
                PondMovement.source_pond_id,
                PondMovement.lot_id,
                func.coalesce(func.sum(PondMovement.fish_quantity), 0),
            )
            .filter(
                PondMovement.source_pond_id.isnot(None),
                PondMovement.destiny_pond_id.isnot(None),
                PondMovement.lot_id.isnot(None),
                PondMovement.movement_time >= start_inclusive,
                PondMovement.movement_time < end_exclusive,
                PondMovement.movement_reason.notin_(["mortality", "faena"]),
            )
            .group_by(PondMovement.source_pond_id, PondMovement.lot_id)
            .all()
        )
        return {
            (int(pond_id), int(lot_id)): int(qty or 0)
            for pond_id, lot_id, qty in rows
            if int(qty or 0) != 0
        }

    if mode == "llegadas":
        rows = (
            db.query(
                PondMovement.destiny_pond_id,
                PondMovement.lot_id,
                func.coalesce(func.sum(PondMovement.fish_quantity), 0),
            )
            .filter(
                PondMovement.destiny_pond_id.isnot(None),
                PondMovement.source_pond_id.isnot(None),
                PondMovement.lot_id.isnot(None),
                PondMovement.movement_time >= start_inclusive,
                PondMovement.movement_time < end_exclusive,
                PondMovement.movement_reason.notin_(["mortality", "faena"]),
            )
            .group_by(PondMovement.destiny_pond_id, PondMovement.lot_id)
            .all()
        )
        return {
            (int(pond_id), int(lot_id)): int(qty or 0)
            for pond_id, lot_id, qty in rows
            if int(qty or 0) != 0
        }

    return {}


def _build_pond_lot_weight_snapshot(
    pond_lot_pairs: set[tuple[int, int]],
    snapshot_dt: datetime,
    db: Session,
) -> dict[tuple[int, int], float]:
    if not pond_lot_pairs:
        return {}

    snapshot_date = snapshot_dt.date() if isinstance(snapshot_dt, datetime) else snapshot_dt

    pond_ids = {pair[0] for pair in pond_lot_pairs}
    lot_ids = {pair[1] for pair in pond_lot_pairs}

    # Fuente primaria: biomass_checkpoints de tipo real, el más reciente <= snapshot_date.
    # Tiene prioridad sobre pond_lot_stats porque es date-specific y ya integra fish_sampling.
    ck_rows = (
        db.query(
            BiomassCheckpoint.pond_id,
            BiomassCheckpoint.lot_id,
            BiomassCheckpoint.avg_weight_g,
        )
        .filter(
            BiomassCheckpoint.pond_id.in_(pond_ids),
            BiomassCheckpoint.lot_id.in_(lot_ids),
            BiomassCheckpoint.checkpoint_type.in_(["sampling", "fish_sampling"]),
            BiomassCheckpoint.avg_weight_g.isnot(None),
            BiomassCheckpoint.checkpoint_date <= snapshot_date,
        )
        .order_by(
            BiomassCheckpoint.pond_id.asc(),
            BiomassCheckpoint.lot_id.asc(),
            BiomassCheckpoint.checkpoint_date.desc(),
        )
        .all()
    )

    checkpoint_map: dict[tuple[int, int], float] = {}
    for pond_id, lot_id, avg_weight_g in ck_rows:
        key = (int(pond_id), int(lot_id))
        if key in pond_lot_pairs and key not in checkpoint_map:
            checkpoint_map[key] = float(avg_weight_g)

    # Fuente secundaria: pond_lot_stats para pares sin checkpoint real previo.
    missing = pond_lot_pairs - checkpoint_map.keys()
    fallback_map: dict[tuple[int, int], float] = {}
    before_map: dict[tuple[int, int], float] = {}
    if missing:
        missing_pond_ids = {p for p, _ in missing}
        missing_lot_ids  = {l for _, l in missing}
        rows = (
            db.query(
                PondLotStats.pond_id,
                PondLotStats.lot_id,
                PondLotStats.avg_weight,
                PondLotStats.sampled_at,
                PondLotStats.updated_at,
                PondLotStats.id,
            )
            .filter(
                PondLotStats.pond_id.in_(missing_pond_ids),
                PondLotStats.lot_id.in_(missing_lot_ids),
                PondLotStats.avg_weight.isnot(None),
            )
            .order_by(
                PondLotStats.pond_id.asc(),
                PondLotStats.lot_id.asc(),
                PondLotStats.sampled_at.desc().nullslast(),
                PondLotStats.updated_at.desc().nullslast(),
                PondLotStats.id.desc(),
            )
            .all()
        )
        for pond_id, lot_id, avg_weight, sampled_at, updated_at, _row_id in rows:
            key = (int(pond_id), int(lot_id))
            if key not in missing:
                continue
            w = float(avg_weight)
            effective_date = sampled_at if sampled_at is not None else (updated_at.date() if updated_at else None)
            if key not in fallback_map:
                fallback_map[key] = w
            if key not in before_map and effective_date and effective_date <= snapshot_date:
                before_map[key] = w

    out: dict[tuple[int, int], float] = {}
    for key in pond_lot_pairs:
        out[key] = checkpoint_map.get(key) or before_map.get(key) or fallback_map.get(key, 0.0)
    return out


def _aggregate_pond_totals(pond_lot_map: dict[tuple[int, int], int]) -> dict[int, int]:
    totals: dict[int, int] = defaultdict(int)
    for (pond_id, _lot_id), qty in pond_lot_map.items():
        totals[int(pond_id)] += int(qty or 0)
    return dict(totals)


def _aggregate_lot_totals(pond_lot_map: dict[tuple[int, int], int]) -> dict[int, int]:
    totals: dict[int, int] = defaultdict(int)
    for (_pond_id, lot_id), qty in pond_lot_map.items():
        totals[int(lot_id)] += int(qty or 0)
    return dict(totals)


def _build_current_pond_lot_counts(db: Session) -> dict[tuple[int, int], int]:
    """Conteo operacional actual por estanque/lote (tagged + sin registrar)."""
    latest_id_subq = _latest_movement_id_per_fish_subq(db)

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

    # Saldo unregistered operacional actual por estanque/lote, obtenido desde el cache del estanque.
    # Este valor representa "hoy" y evita arrastrar balances historicos negativos/positivos ya cerrados.
    unreg_balance_map: dict[tuple[int, int], int] = {}
    for pond_id, balances in db.query(Pond.id, Pond.unregistered_balances_by_lot).all():
        if not balances or not isinstance(balances, dict):
            continue
        for lot_key, balance in balances.items():
            try:
                lot_id = int(lot_key)
                qty = int(balance or 0)
            except (TypeError, ValueError):
                continue
            unreg_balance_map[(int(pond_id), lot_id)] = qty

    out: dict[tuple[int, int], int] = {}
    for key in set(tagged_map) | set(unreg_balance_map):
        tagged_qty = tagged_map.get(key, 0)
        unreg_qty = max(0, unreg_balance_map.get(key, 0))
        total = tagged_qty + unreg_qty
        if total > 0:
            out[key] = total
    return out


def _compute_biomass_for_lot_counts(counts: dict[int, int], lot_weights_g: dict[int, float]) -> dict[int, float]:
    out: dict[int, float] = {}
    for lot_id, qty in counts.items():
        w = float(lot_weights_g.get(lot_id, 0.0))
        out[lot_id] = max(float(qty), 0.0) * w / 1000.0
    return out


def _compute_biomass_for_pond_lot_counts(
    counts: dict[tuple[int, int], int],
    pond_lot_weights_g: dict[tuple[int, int], float],
    lot_fallback_weights_g: dict[int, float],
) -> dict[int, float]:
    by_pond: dict[int, float] = defaultdict(float)
    for (pond_id, lot_id), qty in counts.items():
        if qty <= 0:
            continue
        # dict.get(key, default) no activa el default si la clave existe con valor 0.0
        # (ocurre cuando _build_pond_lot_weight_snapshot pre-llena todas las claves).
        # `or` sí activa el fallback cuando el valor es 0.0.
        w = pond_lot_weights_g.get((pond_id, lot_id)) or lot_fallback_weights_g.get(lot_id, 0.0)
        by_pond[pond_id] += float(qty) * float(w or 0.0) / 1000.0
    return dict(by_pond)


def _get_jaula_quality_conflict_rows(db: Session) -> list[dict]:
    """Peces activos en Jaula con conflicto de datos para faena (sin peso o sin diametro en hembras)."""
    jaula_units = (
        db.query(CultivationUnit)
        .filter(func.lower(CultivationUnit.name).like("%jaula%"))
        .all()
    )
    if not jaula_units:
        return []

    jaula_unit_ids = [u.id for u in jaula_units]
    jaula_ponds = db.query(Pond).filter(Pond.cultivation_unit_id.in_(jaula_unit_ids)).all()
    if not jaula_ponds:
        return []

    jaula_pond_ids = [p.id for p in jaula_ponds]
    jaula_pond_name_map = {p.id: p.name for p in jaula_ponds}

    latest_mv_subq = _latest_movement_id_per_fish_subq(db)
    fish_rows = (
        db.query(Fish, PondMovement.destiny_pond_id)
        .join(latest_mv_subq, latest_mv_subq.c.fish_id == Fish.id)
        .join(PondMovement, PondMovement.id == latest_mv_subq.c.max_id)
        .filter(
            PondMovement.destiny_pond_id.in_(jaula_pond_ids),
            Fish.state.notin_(["dead", "faena", "in_process", "processed"]),
            Fish.internal_id.isnot(None),
            Fish.internal_id != "",
            ~func.upper(Fish.internal_id).like("%\\_R", escape="\\"),
        )
        .order_by(Fish.internal_id.asc(), Fish.id.asc())
        .all()
    )
    if not fish_rows:
        return []

    fish_ids = [fish.id for fish, _pond_id in fish_rows]
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
            (latest_sampling_subq.c.fish_id == FishSampling.fish_id)
            & (latest_sampling_subq.c.max_sample_time == func.coalesce(FishSampling.registry_time, FishSampling.created_at)),
        )
        .all()
    )
    samples_map = {s.fish_id: s for s in latest_samples}

    lot_ids = {fish.lot_id for fish, _pond_id in fish_rows if fish.lot_id}
    lots_map = {lot.id: lot for lot in db.query(Lot).filter(Lot.id.in_(lot_ids)).all()} if lot_ids else {}

    rows = []
    for fish, current_pond_id in fish_rows:
        sample = samples_map.get(fish.id)
        missing_weight = sample is None or sample.weight is None
        sex_norm = _normalize_sex_value(fish.sex)
        missing_diameter = sex_norm == "f" and (sample is None or sample.diameter is None)
        if not missing_weight and not missing_diameter:
            continue

        lot = lots_map.get(fish.lot_id) if fish.lot_id else None
        lot_label = (lot.internal_id or lot.name) if lot else "N/D"

        conflict_labels = []
        if missing_weight:
            conflict_labels.append("sin peso")
        if missing_diameter:
            conflict_labels.append("sin diametro de ovas")

        rows.append(
            {
                "fish_id": fish.id,
                "internal_id": _normalize_pit_tag(fish.internal_id),
                "state": fish.state or "N/D",
                "sex": sex_norm or "",
                "lot": lot_label,
                "current_pond": jaula_pond_name_map.get(current_pond_id) or "N/D",
                "missing_weight": missing_weight,
                "missing_diameter": missing_diameter,
                "conflict_label": ", ".join(conflict_labels),
            }
        )

    rows.sort(key=lambda item: ((item.get("current_pond") or "").lower(), (item.get("internal_id") or "").lower()))
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
    query = db.query(Pond).filter(Pond.state != "inactive")
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
    query = db.query(Pond).filter(Pond.state != "inactive")
    if cultivation_unit_id:
        query = query.filter(Pond.cultivation_unit_id == cultivation_unit_id)
    ponds_db = query.order_by(Pond.id).all()

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

    # ── Agrupar por unidad de cultivo (nivel superior colapsable) ──
    pond_obj_by_id = {p.id: p for p in ponds_db}
    cu_name_by_id = {u.id: u.name for u in cultivation_units}

    grouped_units_map: dict[str, dict] = {}
    for row in grouped:
        pond_obj = pond_obj_by_id.get(row["id"])
        cu_id = pond_obj.cultivation_unit_id if pond_obj else None
        cu_name = cu_name_by_id.get(cu_id, "Sin unidad") if cu_id else "Sin unidad"
        cu_key = str(cu_id) if cu_id is not None else "none"

        if cu_key not in grouped_units_map:
            grouped_units_map[cu_key] = {
                "id": cu_key,
                "name": cu_name,
                "ponds": [],
                "n_fish": 0,
                "biomass": None,
                "avg_condition_k": None,
                "density": None,
                "avg_weight": None,
                "active_lots": [],
            }

        unit_row = grouped_units_map[cu_key]
        unit_row["ponds"].append(row)
        unit_row["n_fish"] += int(row.get("n_fish") or 0)

        cur_biomass = unit_row.get("biomass")
        row_biomass = row.get("biomass")
        if row_biomass is not None:
            unit_row["biomass"] = float(cur_biomass or 0.0) + float(row_biomass)

    # Densidad, peso promedio y lotes por unidad
    for unit_row in grouped_units_map.values():
        rows = unit_row["ponds"]

        total_volume = sum((r.get("volume") or 0) for r in rows if r.get("biomass") is not None)
        total_biomass = unit_row.get("biomass")
        unit_row["density"] = round(float(total_biomass) / total_volume, 2) if (total_biomass is not None and total_volume > 0) else None

        w_sum = 0.0
        w_cnt = 0
        for r in rows:
            avg_w = r.get("avg_weight")
            n_fish = int(r.get("n_fish") or 0)
            if avg_w is None or n_fish <= 0:
                continue
            w_sum += float(avg_w) * n_fish
            w_cnt += n_fish
        unit_row["avg_weight"] = round(w_sum / w_cnt, 1) if w_cnt > 0 else None

        lots_by_id: dict[int, dict] = {}
        for r in rows:
            for lot in r.get("active_lots", []):
                lot_id = int(lot["id"])
                existing = lots_by_id.get(lot_id)
                if not existing:
                    lots_by_id[lot_id] = {
                        "id": lot_id,
                        "name": lot.get("name"),
                        "internal_id": lot.get("internal_id"),
                        "is_unregistered_lot": bool(lot.get("is_unregistered_lot")),
                    }
                else:
                    existing["is_unregistered_lot"] = bool(existing.get("is_unregistered_lot") or lot.get("is_unregistered_lot"))

        unit_row["active_lots"] = list(lots_by_id.values())

    # K por unidad (promedio ponderado por n_fish)
    for unit_row in grouped_units_map.values():
        k_weighted_sum = 0.0
        k_weight = 0
        for pond_row in unit_row["ponds"]:
            pond_k = pond_row.get("avg_condition_k")
            pond_n = int(pond_row.get("n_fish") or 0)
            if pond_k is None or pond_n <= 0:
                continue
            k_weighted_sum += float(pond_k) * pond_n
            k_weight += pond_n
        unit_row["avg_condition_k"] = round(k_weighted_sum / k_weight, 3) if k_weight > 0 else None

    ordered_unit_keys = [str(u.id) for u in cultivation_units if str(u.id) in grouped_units_map]
    if "none" in grouped_units_map:
        ordered_unit_keys.append("none")
    grouped_units = [grouped_units_map[key] for key in ordered_unit_keys]

    # Tarjetas de resumen
    total_fish = sum(s["n_fish"] for s in summaries)
    total_biomass = sum(s["biomass"] for s in summaries if s["biomass"])
    total_volume = sum(s["volume"] for s in summaries if s.get("volume") and s["volume"] > 0 and s["biomass"])
    global_density = round(total_biomass / total_volume, 2) if total_volume else None
    k_values = [s["avg_condition_k"] for s in summaries if s.get("avg_condition_k")]
    avg_k = round(sum(k_values) / len(k_values), 3) if k_values else None
    pending_drug_logs_count = _get_pending_drug_log_fish_query(db).count()

    # Informe sanitario del centro — alerta si vence en < 2 meses
    _latest_san = (
        db.query(SanitaryReport)
        .order_by(SanitaryReport.report_date.desc())
        .first()
    )
    from datetime import date as _date2, timedelta as _td2
    _san_expiry = (_latest_san.report_date + _td2(days=365)) if _latest_san and _latest_san.report_date else None
    sanitary_expiry_warning_ponds = bool(_san_expiry and _san_expiry <= (_date2.today() + _td2(days=60)))

    # Lista flat de estanques sin padre para el selector del formulario de creación
    all_parent_ponds = [
        {"id": p.id, "name": p.name}
        for p in ponds_db
        if p.parent_pond_id is None
    ]

    jaula_quality_conflict_count = len(_get_jaula_quality_conflict_rows(db))

    context = {
        "request": request,
        "ponds": grouped,
        "grouped_units": grouped_units,
        "total_pond_rows": len(grouped),
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
        "sanitary_expiry_warning": sanitary_expiry_warning_ponds,
        "sanitary_expiry_date": _san_expiry,
        "jaula_quality_conflict_count": jaula_quality_conflict_count,
    }
    from app.services.sexado_sessions import locked_pond_ids as _locked_ids
    context["locked_pond_ids"] = list(_locked_ids(db))
    template = jinja_env.get_template("ponds.html")
    html = template.render(context)
    return HTMLResponse(content=html)


REPORT_CHOICES = [
    ("existencia_lote",      "Existencia por Lote"),
    ("existencia_estanque",  "Existencia por Estanque"),
    ("alimento_estanque",    "Alimento por Estanque"),
    ("alimento_lote",        "Alimento por Lote"),
]
_VALID_REPORTS = {r for r, _ in REPORT_CHOICES}


@router.get("/ui/reports", response_class=HTMLResponse)
def ui_reports(
    request: Request,
    month: Optional[str] = None,
    report: str = "existencia_lote",
    db: Session = Depends(get_db),
):
    if report not in _VALID_REPORTS:
        report = "existencia_lote"
    month_value, month_start, month_next, month_end = _parse_month_for_reports(month)

    # Si se consulta el mes en curso, cortar al instante actual para que el
    # cierre "final" sea consistente con la vista operativa de estanques.
    now_utc = datetime.utcnow()
    is_current_month = month_start.year == now_utc.year and month_start.month == now_utc.month
    if is_current_month:
        report_end_exclusive = now_utc + timedelta(seconds=1)
        report_end_snapshot = now_utc
    else:
        report_end_exclusive = month_next
        report_end_snapshot = month_end

    # Reporte 1: Existencia por lotes (nivel centro)
    initial_lot_counts = _sum_center_lot_balance_until(month_start, db)
    if is_current_month:
        current_pond_lot_counts = _build_current_pond_lot_counts(db)
        final_lot_counts = _aggregate_lot_totals(current_pond_lot_counts)
        # Usar tagged-count como referencia de movimientos para evitar
        # sobreconteo de peces que ciclaron por estanques en el mes actual.
        movement_final_lot_counts = _aggregate_lot_totals(current_pond_lot_counts)
    else:
        current_pond_lot_counts = None
        final_lot_counts = _sum_center_lot_balance_until(report_end_exclusive, db)
        movement_final_lot_counts = _sum_center_lot_balance_until(report_end_exclusive, db)
    mortality_lot_counts = _sum_center_lot_reason_between(month_start, report_end_exclusive, "mortality", db)
    faena_lot_counts = _sum_center_lot_reason_between(month_start, report_end_exclusive, "faena", db)

    lot_ids = set(initial_lot_counts.keys()) | set(final_lot_counts.keys()) | set(mortality_lot_counts.keys()) | set(faena_lot_counts.keys())
    lots_map = {
        lot.id: lot
        for lot in db.query(Lot).filter(Lot.id.in_(lot_ids)).all()
    } if lot_ids else {}

    # Biomasa inicial: leer desde checkpoint si existe (inmutable), si no calcular on-the-fly.
    initial_lot_biomass, _initial_pond_biomass_ck = _read_biomass_checkpoint(
        month_start.date(), "month_start", db
    )
    if not initial_lot_biomass:
        initial_pond_lot_for_biomass = _sum_pond_lot_balance_until(month_start, db)
        pond_lot_weights_start = _build_pond_lot_weight_snapshot(
            set(initial_pond_lot_for_biomass.keys()), month_start, db
        )
        lot_weights_start_fallback = _build_lot_weight_snapshot(lot_ids, month_start, db)
        initial_lot_biomass = defaultdict(float)
        for (pid, lid), qty in initial_pond_lot_for_biomass.items():
            if qty > 0:
                w = pond_lot_weights_start.get((pid, lid)) or lot_weights_start_fallback.get(lid, 0.0)
                initial_lot_biomass[lid] += qty * w / 1000.0

    # Mortalidad y faena: peso aproximado al final del período (menor impacto, peso único por lote)
    lot_weights_end = _build_lot_weight_snapshot(lot_ids, report_end_snapshot, db)
    mortality_lot_biomass = _compute_biomass_for_lot_counts(mortality_lot_counts, lot_weights_end)
    faena_lot_biomass = _compute_biomass_for_lot_counts(faena_lot_counts, lot_weights_end)

    biomass_alerts: list[str] = []
    if is_current_month:
        _pond_lot_proj, biomass_alerts = _project_biomass_from_checkpoint(report_end_snapshot.date(), db)
        final_lot_biomass: dict[int, float] = defaultdict(float)
        for (_pid, _lid), _bm in _pond_lot_proj.items():
            final_lot_biomass[_lid] += _bm
    else:
        final_lot_biomass, _ = _read_biomass_checkpoint(
            report_end_snapshot.date(), "month_start", db
        )
        if not final_lot_biomass:
            final_pond_lot_for_biomass = _sum_pond_lot_balance_until(report_end_exclusive, db)
            pond_lot_weights_end = _build_pond_lot_weight_snapshot(
                set(final_pond_lot_for_biomass.keys()), report_end_snapshot, db
            )
            final_lot_biomass = defaultdict(float)
            for (pid, lid), qty in final_pond_lot_for_biomass.items():
                if qty > 0:
                    w = pond_lot_weights_end.get((pid, lid)) or lot_weights_end.get(lid, 0.0)
                    final_lot_biomass[lid] += qty * w / 1000.0

    lot_rows = []
    for lot_id in sorted(lot_ids, key=lambda lid: ((lots_map.get(lid).name or str(lid)).lower() if lots_map.get(lid) else str(lid))):
        lot = lots_map.get(lot_id)
        lot_label = lot.name if lot else f"Lote {lot_id}"
        lot_rows.append({
            "lot_id": lot_id,
            "lot_label": lot_label,
            "initial_qty": int(initial_lot_counts.get(lot_id, 0)),
            "initial_biomass_kg": round(float(initial_lot_biomass.get(lot_id, 0.0)), 1),
            "mortality_qty": int(mortality_lot_counts.get(lot_id, 0)),
            "mortality_biomass_kg": round(float(mortality_lot_biomass.get(lot_id, 0.0)), 1),
            "faena_qty": int(faena_lot_counts.get(lot_id, 0)),
            "faena_biomass_kg": round(float(faena_lot_biomass.get(lot_id, 0.0)), 1),
            "final_qty": int(final_lot_counts.get(lot_id, 0)),
            "final_biomass_kg": round(float(final_lot_biomass.get(lot_id, 0.0)), 1),
            "reconciliation_qty": int(final_lot_counts.get(lot_id, 0)) - int(movement_final_lot_counts.get(lot_id, 0)),
        })

    lot_totals = {
        "initial_qty": sum(r["initial_qty"] for r in lot_rows),
        "initial_biomass_kg": round(sum(r["initial_biomass_kg"] for r in lot_rows), 1),
        "mortality_qty": sum(r["mortality_qty"] for r in lot_rows),
        "mortality_biomass_kg": round(sum(r["mortality_biomass_kg"] for r in lot_rows), 1),
        "faena_qty": sum(r["faena_qty"] for r in lot_rows),
        "faena_biomass_kg": round(sum(r["faena_biomass_kg"] for r in lot_rows), 1),
        "final_qty": sum(r["final_qty"] for r in lot_rows),
        "final_biomass_kg": round(sum(r["final_biomass_kg"] for r in lot_rows), 1),
        "reconciliation_qty": sum(r["reconciliation_qty"] for r in lot_rows),
    }

    # Reporte 2: Existencia por estanque
    initial_pond_lot = _sum_pond_lot_balance_until(month_start, db)
    if is_current_month:
        # Para el mes en curso usar tagged-count (último movimiento por pez) como referencia
        # de movimientos, evitando sobreconteo de peces que ciclaron por un estanque.
        final_pond_lot = current_pond_lot_counts or {}
        movement_final_pond_lot = current_pond_lot_counts or {}
    else:
        movement_final_pond_lot = _sum_pond_lot_balance_until(report_end_exclusive, db)
        final_pond_lot = _sum_pond_lot_balance_until(report_end_exclusive, db)
    mortality_pond_lot = _sum_pond_lot_between(month_start, report_end_exclusive, "mortality", db)
    faena_pond_lot = _sum_pond_lot_between(month_start, report_end_exclusive, "faena", db)
    salidas_pond_lot = _sum_pond_lot_between(month_start, report_end_exclusive, "salidas", db)
    llegadas_pond_lot = _sum_pond_lot_between(month_start, report_end_exclusive, "llegadas", db)

    pond_lot_pairs = (
        set(initial_pond_lot.keys())
        | set(final_pond_lot.keys())
        | set(mortality_pond_lot.keys())
        | set(faena_pond_lot.keys())
        | set(salidas_pond_lot.keys())
        | set(llegadas_pond_lot.keys())
    )

    pond_ids = {pair[0] for pair in pond_lot_pairs}
    pond_map = {
        pond.id: pond
        for pond in db.query(Pond).filter(Pond.id.in_(pond_ids)).all()
    } if pond_ids else {}

    lot_ids_for_pond_report = {pair[1] for pair in pond_lot_pairs}
    lot_weights_start_for_pond = _build_lot_weight_snapshot(lot_ids_for_pond_report, month_start, db)
    lot_weights_end_for_pond = _build_lot_weight_snapshot(lot_ids_for_pond_report, report_end_snapshot, db)
    pond_lot_weights_start = _build_pond_lot_weight_snapshot(pond_lot_pairs, month_start, db)
    pond_lot_weights_end = _build_pond_lot_weight_snapshot(pond_lot_pairs, report_end_snapshot, db)

    initial_pond_counts = _aggregate_pond_totals(initial_pond_lot)
    movement_final_pond_counts = _aggregate_pond_totals(movement_final_pond_lot)
    final_pond_counts = _aggregate_pond_totals(final_pond_lot)
    mortality_pond_counts = _aggregate_pond_totals(mortality_pond_lot)
    faena_pond_counts = _aggregate_pond_totals(faena_pond_lot)
    salidas_pond_counts = _aggregate_pond_totals(salidas_pond_lot)
    llegadas_pond_counts = _aggregate_pond_totals(llegadas_pond_lot)

    # Biomasa inicial de estanques: leer desde checkpoint si existe, si no calcular on-the-fly.
    _, initial_pond_biomass_ck = _read_biomass_checkpoint(month_start.date(), "month_start", db)
    if initial_pond_biomass_ck:
        initial_pond_biomass = initial_pond_biomass_ck
    else:
        initial_pond_biomass = _compute_biomass_for_pond_lot_counts(initial_pond_lot, pond_lot_weights_start, lot_weights_start_for_pond)

    if is_current_month:
        # Reusar _pond_lot_proj calculado para existencia_lote (garantiza Σlote == Σestanque)
        final_pond_biomass: dict[int, float] = defaultdict(float)
        for (_pid, _lid), _bm in _pond_lot_proj.items():
            final_pond_biomass[_pid] += _bm
    else:
        _, final_pond_biomass_ck = _read_biomass_checkpoint(report_end_snapshot.date(), "month_start", db)
        if final_pond_biomass_ck:
            final_pond_biomass = final_pond_biomass_ck
        else:
            final_pond_biomass = _compute_biomass_for_pond_lot_counts(final_pond_lot, pond_lot_weights_end, lot_weights_end_for_pond)
    mortality_pond_biomass = _compute_biomass_for_pond_lot_counts(mortality_pond_lot, pond_lot_weights_end, lot_weights_end_for_pond)
    faena_pond_biomass = _compute_biomass_for_pond_lot_counts(faena_pond_lot, pond_lot_weights_end, lot_weights_end_for_pond)
    salidas_pond_biomass = _compute_biomass_for_pond_lot_counts(salidas_pond_lot, pond_lot_weights_end, lot_weights_end_for_pond)
    llegadas_pond_biomass = _compute_biomass_for_pond_lot_counts(llegadas_pond_lot, pond_lot_weights_end, lot_weights_end_for_pond)

    pond_row_ids = (
        set(initial_pond_counts.keys())
        | set(final_pond_counts.keys())
        | set(mortality_pond_counts.keys())
        | set(faena_pond_counts.keys())
        | set(salidas_pond_counts.keys())
        | set(llegadas_pond_counts.keys())
    )

    pond_rows = []
    for pond_id in sorted(pond_row_ids, key=lambda pid: ((pond_map.get(pid).name or f"Estanque {pid}").lower() if pond_map.get(pid) else str(pid))):
        pond = pond_map.get(pond_id)
        pond_rows.append({
            "pond_id": pond_id,
            "pond_name": pond.name if pond else f"Estanque {pond_id}",
            "initial_qty": int(initial_pond_counts.get(pond_id, 0)),
            "initial_biomass_kg": round(float(initial_pond_biomass.get(pond_id, 0.0)), 1),
            "mortality_qty": int(mortality_pond_counts.get(pond_id, 0)),
            "mortality_biomass_kg": round(float(mortality_pond_biomass.get(pond_id, 0.0)), 1),
            "faena_qty": int(faena_pond_counts.get(pond_id, 0)),
            "faena_biomass_kg": round(float(faena_pond_biomass.get(pond_id, 0.0)), 1),
            "salidas_qty": int(salidas_pond_counts.get(pond_id, 0)),
            "salidas_biomass_kg": round(float(salidas_pond_biomass.get(pond_id, 0.0)), 1),
            "llegadas_qty": int(llegadas_pond_counts.get(pond_id, 0)),
            "llegadas_biomass_kg": round(float(llegadas_pond_biomass.get(pond_id, 0.0)), 1),
            "final_qty": int(final_pond_counts.get(pond_id, 0)),
            "final_biomass_kg": round(float(final_pond_biomass.get(pond_id, 0.0)), 1),
            "reconciliation_qty": int(final_pond_counts.get(pond_id, 0)) - int(movement_final_pond_counts.get(pond_id, 0)),
        })

    pond_totals = {
        "initial_qty": sum(r["initial_qty"] for r in pond_rows),
        "initial_biomass_kg": round(sum(r["initial_biomass_kg"] for r in pond_rows), 1),
        "mortality_qty": sum(r["mortality_qty"] for r in pond_rows),
        "mortality_biomass_kg": round(sum(r["mortality_biomass_kg"] for r in pond_rows), 1),
        "faena_qty": sum(r["faena_qty"] for r in pond_rows),
        "faena_biomass_kg": round(sum(r["faena_biomass_kg"] for r in pond_rows), 1),
        "salidas_qty": sum(r["salidas_qty"] for r in pond_rows),
        "salidas_biomass_kg": round(sum(r["salidas_biomass_kg"] for r in pond_rows), 1),
        "llegadas_qty": sum(r["llegadas_qty"] for r in pond_rows),
        "llegadas_biomass_kg": round(sum(r["llegadas_biomass_kg"] for r in pond_rows), 1),
        "final_qty": sum(r["final_qty"] for r in pond_rows),
        "final_biomass_kg": round(sum(r["final_biomass_kg"] for r in pond_rows), 1),
        "reconciliation_qty": sum(r["reconciliation_qty"] for r in pond_rows),
    }

    feed_data = None
    feed_lot_data = None
    if report in ("alimento_estanque", "alimento_lote"):
        feed_data = _build_feed_monthly_matrix(month_start.year, month_start.month, db)
        feed_lot_data = (
            _build_feed_lot_allocation(month_start.year, month_start.month, feed_data, db)
            if feed_data else None
        )

    template = jinja_env.get_template("reports.html")
    html = template.render(
        {
            "request": request,
            "month": month_value,
            "month_start": month_start,
            "month_end": report_end_snapshot,
            "report": report,
            "report_choices": REPORT_CHOICES,
            "lot_rows": lot_rows,
            "lot_totals": lot_totals,
            "pond_rows": pond_rows,
            "pond_totals": pond_totals,
            "feed_data": feed_data,
            "feed_lot_data": feed_lot_data,
            "biomass_alerts": biomass_alerts,
        }
    )
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Reporte mensual de alimentación
# ---------------------------------------------------------------------------

def _build_feed_monthly_matrix(year: int, month: int, db: Session):
    from calendar import monthrange

    matrix = defaultdict(lambda: defaultdict(float))
    data_sources = set()

    # Datos legacy
    for row in db.query(FeedMonthlyConsumptionLegacy).filter(
        FeedMonthlyConsumptionLegacy.year == year,
        FeedMonthlyConsumptionLegacy.month == month,
    ).all():
        matrix[row.pond_code][row.feed_type_key] += row.consumed_kg
        data_sources.add("legacy")

    # Datos operativos (FeedExecutionEvent)
    month_start = datetime(year, month, 1)
    _, last_day = monthrange(year, month)
    month_end_dt = datetime(year, month, last_day, 23, 59, 59)

    # Peso por saco por feed_type (del recibo confirmado más reciente).
    # Necesario porque confirmed_kg puede estar en 0 si el sistema no lo calculó.
    bag_weight_by_type: dict[int, float] = {}
    for rl in (
        db.query(FeedReceiptLine.feed_type_id, FeedReceiptLine.bag_weight_kg)
        .join(FeedReceiptHeader, FeedReceiptHeader.id == FeedReceiptLine.feed_receipt_id)
        .filter(FeedReceiptHeader.status == "confirmed")
        .order_by(FeedReceiptLine.feed_type_id, FeedReceiptHeader.fecha_ingreso.desc())
        .distinct(FeedReceiptLine.feed_type_id)
        .all()
    ):
        bag_weight_by_type[int(rl.feed_type_id)] = float(rl.bag_weight_kg)

    op_rows = (
        db.query(
            Pond.internal_id.label("pond_code"),
            FeedType.name.label("feed_type_name"),
            FeedExecutionEvent.feed_type_id,
            func.sum(FeedExecutionEvent.confirmed_bags).label("total_bags"),
            func.sum(FeedExecutionEvent.confirmed_kg).label("total_kg"),
        )
        .join(Pond, Pond.id == FeedExecutionEvent.pond_id)
        .join(FeedType, FeedType.id == FeedExecutionEvent.feed_type_id)
        .filter(
            FeedExecutionEvent.confirmed_at >= month_start,
            FeedExecutionEvent.confirmed_at <= month_end_dt,
        )
        .group_by(Pond.internal_id, FeedType.name, FeedExecutionEvent.feed_type_id)
        .all()
    )
    for row in op_rows:
        key = OPERATIONAL_TO_FEED_KEY.get(row.feed_type_name,
              row.feed_type_name.lower().replace(" ", "_"))
        total_kg = float(row.total_kg or 0)
        if total_kg == 0:
            bw = bag_weight_by_type.get(int(row.feed_type_id), 25.0)
            total_kg = int(row.total_bags) * bw
        matrix[row.pond_code][key] += total_kg
        data_sources.add("operativo")

    if not matrix:
        return None

    # Columnas con datos, en orden natural de pellet
    all_keys = {k for pond in matrix.values() for k in pond}
    ordered_keys = [k for k, _ in FEED_TYPE_DISPLAY_ORDER if k in all_keys]
    for k in sorted(all_keys - set(ordered_keys)):
        ordered_keys.append(k)

    # Totales por columna
    col_totals = {k: round(sum(matrix[p].get(k, 0) for p in matrix), 1)
                  for k in ordered_keys}

    # Agrupar estanques por unidad de cultivo
    pond_codes = list(matrix.keys())
    pond_cu = {
        row.internal_id: row.cu_name
        for row in db.query(Pond.internal_id, CultivationUnit.name.label("cu_name"))
        .join(CultivationUnit, CultivationUnit.id == Pond.cultivation_unit_id)
        .filter(Pond.internal_id.in_(pond_codes))
        .all()
    }

    cu_groups: dict[str, list[str]] = defaultdict(list)
    for code in pond_codes:
        cu_groups[pond_cu.get(code, "Sin clasificar")].append(code)
    for cu in cu_groups:
        cu_groups[cu].sort()

    ordered_groups = [(cu, cu_groups[cu]) for cu in CU_DISPLAY_ORDER if cu in cu_groups]
    for cu in sorted(cu_groups):
        if cu not in CU_DISPLAY_ORDER:
            ordered_groups.append((cu, cu_groups[cu]))

    # Subtotales por grupo
    group_totals = {}
    for cu_name, codes in ordered_groups:
        group_totals[cu_name] = {
            k: round(sum(matrix[p].get(k, 0) for p in codes), 1)
            for k in ordered_keys
        }
        group_totals[cu_name]["__total__"] = round(
            sum(group_totals[cu_name][k] for k in ordered_keys), 1
        )

    return {
        "matrix": dict(matrix),
        "columns": ordered_keys,
        "column_labels": {k: FEED_TYPE_DISPLAY.get(k, k) for k in ordered_keys},
        "groups": ordered_groups,
        "group_totals": group_totals,
        "col_totals": col_totals,
        "grand_total": round(sum(col_totals.values()), 1),
        "data_sources": sorted(data_sources),
    }


def _net_fish_per_pond_lot(cutoff: datetime, pond_ids: list[int], db: Session) -> dict[int, dict[int, int]]:
    """Net fish count per (pond_id, lot_id) at a given cutoff (exclusive).
    Uses cumulative entries minus cumulative exits up to that moment.
    """
    if not pond_ids:
        return {}
    result = db.execute(
        text("""
            WITH entries AS (
                SELECT destiny_pond_id AS pond_id, lot_id, SUM(fish_quantity) AS qty
                FROM ponds_movements
                WHERE destiny_pond_id IS NOT NULL AND lot_id IS NOT NULL
                  AND movement_time < :cutoff
                  AND destiny_pond_id = ANY(:pond_ids)
                GROUP BY destiny_pond_id, lot_id
            ),
            exits AS (
                SELECT source_pond_id AS pond_id, lot_id, SUM(fish_quantity) AS qty
                FROM ponds_movements
                WHERE source_pond_id IS NOT NULL AND lot_id IS NOT NULL
                  AND movement_time < :cutoff
                  AND source_pond_id = ANY(:pond_ids)
                GROUP BY source_pond_id, lot_id
            )
            SELECT
                COALESCE(e.pond_id, x.pond_id) AS pond_id,
                COALESCE(e.lot_id,  x.lot_id)  AS lot_id,
                COALESCE(e.qty, 0) - COALESCE(x.qty, 0) AS net_fish
            FROM entries e
            FULL OUTER JOIN exits x ON x.pond_id = e.pond_id AND x.lot_id = e.lot_id
            WHERE COALESCE(e.qty, 0) - COALESCE(x.qty, 0) > 0
        """),
        {"cutoff": cutoff, "pond_ids": pond_ids},
    )
    out: dict[int, dict[int, int]] = defaultdict(dict)
    for row in result:
        out[int(row.pond_id)][int(row.lot_id)] = int(row.net_fish)
    return dict(out)


def _build_feed_lot_allocation(year: int, month: int, feed_data: dict, db: Session):
    """Distribuye los kg de alimento entre lotes según la proporción de peces
    promedio (inicio + fin de mes) que cada lote tenía en cada estanque.
    """
    from calendar import monthrange

    columns = feed_data["columns"]

    # Mapas pond_code <-> pond_id
    pond_codes = list(feed_data["matrix"].keys())
    pond_rows = (
        db.query(Pond.internal_id, Pond.id)
        .filter(Pond.internal_id.in_(pond_codes))
        .all()
    )
    code_to_id = {r.internal_id: int(r.id) for r in pond_rows}
    pond_ids = list(code_to_id.values())

    lot_names: dict[int, str] = {
        int(r.id): r.name
        for r in db.query(Lot.id, Lot.name).all()
    }

    # Hijos directos de los estanques del feed matrix (para fallback cuando padre=0 peces)
    parent_to_children: dict[int, list[int]] = {}
    child_rows = (
        db.query(Pond.id, Pond.parent_pond_id)
        .filter(Pond.parent_pond_id.in_(pond_ids))
        .all()
    )
    child_ids: list[int] = []
    for cr in child_rows:
        pid = int(cr.parent_pond_id)
        cid = int(cr.id)
        parent_to_children.setdefault(pid, []).append(cid)
        child_ids.append(cid)

    # Peces por (pond, lot) al inicio y fin del mes → promedio
    # Se consultan tanto los estanques padre como los hijos
    month_start = datetime(year, month, 1)
    if month == 12:
        month_end = datetime(year + 1, 1, 1)
    else:
        month_end = datetime(year, month + 1, 1)

    all_ids = pond_ids + child_ids
    fish_start = _net_fish_per_pond_lot(month_start, all_ids, db)
    fish_end   = _net_fish_per_pond_lot(month_end,   all_ids, db)

    avg_fish: dict[int, dict[int, float]] = {}
    for pond_id in set(fish_start) | set(fish_end):
        lots_s = fish_start.get(pond_id, {})
        lots_e = fish_end.get(pond_id, {})
        avg: dict[int, float] = {}
        for lot_id in set(lots_s) | set(lots_e):
            a = (lots_s.get(lot_id, 0) + lots_e.get(lot_id, 0)) / 2
            if a > 0:
                avg[lot_id] = a
        if avg:
            avg_fish[pond_id] = avg

    # Asignación proporcional
    allocation: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unallocated: dict[str, float] = defaultdict(float)

    for pond_code, feed_row in feed_data["matrix"].items():
        pond_id = code_to_id.get(pond_code)
        lot_counts = dict(avg_fish.get(pond_id, {})) if pond_id else {}
        total_fish = sum(lot_counts.values())

        # Fallback: si el padre no tiene peces propios pero tiene hijos con peces,
        # distribuir el feed del padre usando los peces agregados de los hijos
        if total_fish == 0 and pond_id in parent_to_children:
            for child_id in parent_to_children[pond_id]:
                for lot_id, count in avg_fish.get(child_id, {}).items():
                    lot_counts[lot_id] = lot_counts.get(lot_id, 0) + count
            total_fish = sum(lot_counts.values())

        for feed_key, kg in feed_row.items():
            if kg <= 0:
                continue
            if total_fish == 0:
                unallocated[feed_key] += kg
                continue
            for lot_id, count in lot_counts.items():
                lot_name = lot_names.get(lot_id, f"Lote {lot_id}")
                allocation[lot_name][feed_key] += kg * (count / total_fish)

    if not allocation:
        return None

    # Redondear y calcular totales
    rows_sorted = {
        lot: {k: round(v, 1) for k, v in feed_map.items()}
        for lot, feed_map in sorted(allocation.items())
    }
    lot_totals = {lot: round(sum(fm.values()), 1) for lot, fm in rows_sorted.items()}
    col_totals = {
        k: round(sum(rows_sorted[lot].get(k, 0) for lot in rows_sorted), 1)
        for k in columns
    }

    return {
        "rows": rows_sorted,
        "lot_totals": lot_totals,
        "col_totals": col_totals,
        "grand_total": round(sum(lot_totals.values()), 1),
        "unallocated": {k: round(v, 1) for k, v in unallocated.items() if v > 0},
        "columns": columns,
        "column_labels": feed_data["column_labels"],
    }


@router.get("/ui/reports/feed-monthly", response_class=HTMLResponse)
def ui_feed_monthly_report(
    request: Request,
    month: Optional[str] = None,
    db: Session = Depends(get_db),
):
    month_value, month_start, _, month_end = _parse_month_for_reports(month)
    feed_data = _build_feed_monthly_matrix(month_start.year, month_start.month, db)
    lot_data = _build_feed_lot_allocation(month_start.year, month_start.month, feed_data, db) \
               if feed_data else None
    template = jinja_env.get_template("feed_monthly_report.html")
    html = template.render({
        "request": request,
        "month": month_value,
        "month_start": month_start,
        "month_end": month_end,
        "feed_data": feed_data,
        "lot_data": lot_data,
    })
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


@router.get("/ui/fish/jaula-quality-conflicts", response_class=HTMLResponse)
def ui_jaula_quality_conflicts(
    request: Request,
    status: Optional[str] = None,
    msg: Optional[str] = None,
    db: Session = Depends(get_db),
):
    rows = _get_jaula_quality_conflict_rows(db)
    template = jinja_env.get_template("fish_jaula_quality_conflicts.html")
    html = template.render(
        {
            "request": request,
            "status": status,
            "msg": msg,
            "rows": rows,
            "pending_count": len(rows),
        }
    )
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
    all_ponds = db.query(Pond).filter(Pond.parent_pond_id.is_(None), Pond.state != "inactive").order_by(Pond.name).all()

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
        .filter(Pond.id != pond_id, Pond.parent_pond_id.is_(None), Pond.state != "inactive")
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

    if pond.unregistered_balances_by_lot is None:
        _refresh_pond_runtime_cache_many([pond_id], db)
        db.flush()
        db.refresh(pond)

    current_fish = _get_current_tagged_fish_in_pond(pond_id, db)
    # Fuente de verdad operacional para sin-tag en esta vista.
    live_unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)
    cached_unregistered_balances = _as_int_dict(pond.unregistered_balances_by_lot)
    live_unregistered_count = int(sum(live_unregistered_balances.values()))
    cached_unregistered_count = int(pond.unregistered_count or 0)

    # Si el cache quedó desfasado, lo re-sincronizamos para evitar mostrar
    # peces sin tag "fantasma" en la UI de detalle.
    if (
        cached_unregistered_balances != live_unregistered_balances
        or cached_unregistered_count != live_unregistered_count
    ):
        _refresh_pond_runtime_cache_many([pond_id], db)
        db.flush()
        db.refresh(pond)

    unregistered_balances = live_unregistered_balances
    unregistered_count = live_unregistered_count

    lot_ids = list({f.lot_id for f in current_fish if f.lot_id})
    lots_map = {
        l.id: l for l in db.query(Lot).filter(Lot.id.in_(lot_ids)).all()
    } if lot_ids else {}

    fish_ids = [f.id for f in current_fish]

    samples_map = {}
    if fish_ids:
        latest_sampling_subq = (
            db.query(
                FishSampling.fish_id,
                func.max(
                    func.coalesce(FishSampling.registry_time, FishSampling.created_at)
                ).label("max_sample_time"),
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
                (
                    latest_sampling_subq.c.max_sample_time ==
                    func.coalesce(FishSampling.registry_time, FishSampling.created_at)
                )
            )
            .filter(FishSampling.fish_id.in_(fish_ids))
            .all()
        )
        samples_map = {s.fish_id: s for s in latest_samples}

    depuration_start_map = {}
    depuration_fish_ids = [f.id for f in current_fish if f.state == "depuration"]
    if depuration_fish_ids:
        movements = (
            db.query(
                PondMovement.fish_id,
                PondMovement.source_pond_id,
                PondMovement.destiny_pond_id,
                PondMovement.movement_time,
                PondMovement.id,
            )
            .filter(PondMovement.fish_id.in_(depuration_fish_ids))
            .order_by(PondMovement.fish_id.asc(), PondMovement.movement_time.asc(), PondMovement.id.asc())
            .all()
        )

        pond_ids = {
            m[1] for m in movements if m[1]
        } | {
            m[2] for m in movements if m[2]
        }
        pond_depuration_map = {
            p.id: bool(p.depuration)
            for p in db.query(Pond).filter(Pond.id.in_(pond_ids)).all()
        } if pond_ids else {}

        cycle_start_by_fish = {}
        for fish_id, source_pond_id, destiny_pond_id, movement_time, _movement_id in movements:
            if fish_id is None:
                continue

            src_is_depuration = bool(
                source_pond_id and pond_depuration_map.get(source_pond_id)
            )
            dst_is_depuration = bool(
                destiny_pond_id and pond_depuration_map.get(destiny_pond_id)
            )

            # La depuración se inicia solo en transición no-depuración -> depuración.
            if (not src_is_depuration) and dst_is_depuration:
                cycle_start_by_fish[fish_id] = movement_time
            # La depuración termina al salir a estanque no-depuración.
            elif src_is_depuration and (not dst_is_depuration):
                cycle_start_by_fish[fish_id] = None

        depuration_start_map = cycle_start_by_fish

    # Parámetros de los modelos (cacheados; no re-ajustan en caliente).
    # Si no hay caché o falla, simplemente no se muestran predicciones.
    try:
        _ovc_params = _ovc.get_params()
    except Exception:
        _ovc_params = None
    try:
        _growth_params = _og.load_params()
    except Exception:
        _growth_params = None

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

        # Calcular días desde último muestreo
        days_since_last_sample = None
        sample_date = None
        if sample and (sample.registry_time or sample.created_at):
            sample_date = (sample.registry_time or sample.created_at).date()
            days_since_last_sample = (datetime.utcnow().date() - sample_date).days

        # Predicción por hembra (estado ≥2).
        #  - Etapa 2/3: e4_label = fecha probable de E4; columna = días a próximo
        #    muestreo (modelo de ciclo ovárico).
        #  - Etapa 4: la decisión es por diámetro de ova (umbral cosecha 2,8 mm);
        #    e4_label = estado ø; columna = días estimados a 2,8 (modelo crecimiento).
        e4_label = None
        next_days_label = None
        _dev = (str(sample.development_state).strip().upper()
                if sample and sample.development_state else "")
        _diam = None
        if sample and sample.diameter is not None:
            try:
                _d = float(sample.diameter)
                _diam = _d if 1.5 <= _d <= 5.0 else None
            except (TypeError, ValueError):
                _diam = None
        if is_female and _dev in ("2", "3", "4"):
            _today = datetime.utcnow().date()
            if _dev == "4":
                if _diam is not None and _diam >= _og.THRESHOLD_MM:
                    e4_label = f"🎯 lista (ø{_diam:.1f})"
                    next_days_label = "ya"
                elif _diam is not None:
                    e4_label = f"ø{_diam:.1f} → 2,8"
                    _nd = (_og.days_to_threshold(_diam, sample_date, _growth_params)
                           if _growth_params and sample_date else None)
                    if _nd is not None:
                        next_days_label = "ya" if _nd <= 0 else str(_nd)
                else:
                    e4_label = "🎯 en 4"
            elif _ovc_params and sample_date:
                _e4d, _nxd = _ovc.predict_for(int(_dev), sample_date, _ovc_params)
                # fecha en el pasado (muestreo antiguo) -> evitar mes ambiguo sin año
                e4_label = ("E4 ¿revisar?" if (_e4d and _e4d < _today)
                            else "E4 " + _ovc.fmt_daymon(_e4d))
                if _nxd:
                    _dd = (_nxd - _today).days
                    next_days_label = "ya" if _dd <= 0 else str(_dd)

        fish_rows.append({
            "fish_id": fish.id,
            "internal_id": fish.internal_id or "N/D",
            "lot": (lot.name or lot.internal_id) if lot else "N/D",
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
            "days_since_last_sample": days_since_last_sample,
            "e4_label": e4_label,
            "next_days_label": next_days_label,
        })

    show_depuration_column = pond.depuration or any(row["depuration_days"] is not None for row in fish_rows)

    all_ponds = db.query(Pond).filter(Pond.state != "inactive").order_by(Pond.name).all()

    # Eventos de pérdida de tag pendientes (sin re-tagear O re-tagueados sin reconciliar) en este estanque
    pending_detachment_events = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status.in_(["unidentified", "retagged"]),
        TagDetachmentEvent.resolved_at.is_(None),
    ).order_by(TagDetachmentEvent.event_date.asc()).all()

    # Retags sin resolver: se restan del total (evitan doble conteo del pez
    # re-tagueado). La lista /ui/ponds ya los descuenta en n_fish_cached; aquí lo
    # exponemos para que el total del detalle cuadre con el de la lista.
    retagged_unresolved_count = sum(
        1 for e in pending_detachment_events if e.status == "retagged"
    )
    net_fish_count = len(fish_rows) + unregistered_count - retagged_unresolved_count

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

    present_lot_ids = sorted(set(lots_map.keys()) | set(unregistered_lots_map.keys()))
    present_lot_options = []
    for lot_id in present_lot_ids:
        lot = lots_map.get(lot_id) or unregistered_lots_map.get(lot_id)
        present_lot_options.append({
            "id": int(lot_id),
            "label": (
                lot.internal_id if lot and lot.internal_id
                else (lot.name if lot and lot.name else f"Lote {lot_id}")
            ),
        })

    tag_detachment_lot_options = []
    for opt in present_lot_options:
        tag_detachment_lot_options.append({
            "id": opt["id"],
            "label": opt["label"],
            "qty": int(unregistered_balances.get(opt["id"], 0)),
            "fallback": int(unregistered_balances.get(opt["id"], 0)) == 0,
        })

    females_state_4_count = sum(
        1
        for row in fish_rows
        if row.get("sex_value") == "f" and row.get("development_state") == "4"
    )

    # Recuento por lote (peces con PIT)
    _lot_counts: dict = defaultdict(int)
    for _f in current_fish:
        _lot = lots_map.get(_f.lot_id)
        _abbr = (_lot.internal_id or _lot.name) if _lot else "N/D"
        _lot_counts[_abbr] += 1
    fish_by_lot = dict(sorted(_lot_counts.items()))

    # Recuento por sexo
    sex_count_f = sum(1 for r in fish_rows if r.get("sex_value") == "f")
    sex_count_m = sum(1 for r in fish_rows if r.get("sex_value") == "m")
    sex_count_u = len(fish_rows) - sex_count_f - sex_count_m

    # Hembras por estado de desarrollo: conteo + biomasa + caviar estimado.
    # El caviar se estima solo en estados maduros (CAVIAR_MATURE_STATES) porque
    # las inmaduras aún no tienen la ova; el factor biomasa→caviar envasado sale
    # de los rendimientos reales de PlantaApp (cacheado, con fallback al último bueno).
    _state_counts: dict = defaultdict(int)
    _state_biomass_kg: dict = defaultdict(float)
    for _r in fish_rows:
        if _r.get("sex_value") == "f":
            _state = _r.get("development_state") or "—"
            _state_counts[_state] += 1
            _state_biomass_kg[_state] += float(_r.get("last_weight") or 0) / 1000.0

    caviar_yield = get_caviar_yield_factor()
    caviar_factor = float(caviar_yield.get("factor") or 0.0)

    females_by_state = {}
    for _state in sorted(_state_counts.keys()):
        _biomass = round(_state_biomass_kg[_state], 1)
        _is_mature = _state in CAVIAR_MATURE_STATES
        females_by_state[_state] = {
            "count": _state_counts[_state],
            "biomass_kg": _biomass,
            "caviar_kg": round(_state_biomass_kg[_state] * caviar_factor, 1) if _is_mature else None,
            "mature": _is_mature,
        }

    mature_biomass_kg = sum(
        _state_biomass_kg[_state] for _state in _state_biomass_kg if _state in CAVIAR_MATURE_STATES
    )
    caviar_estimated_kg = mature_biomass_kg * caviar_factor

    # Bloqueo por sesión de sexado offline (solo lectura)
    from app.services.sexado_sessions import pond_lock_session
    from app.models.users import User as _User
    _lock = pond_lock_session(db, pond_id)
    lock_info = None
    if _lock:
        _op = db.query(_User).filter(_User.id == _lock.operator_id).first() if _lock.operator_id else None
        lock_info = {
            "operator": (" ".join(x for x in [_op.name, _op.lastname] if x) if _op else None) or "—",
            "since": _lock.created_at,
            "is_source": (_lock.source_pond_id == pond_id),
        }

    template = jinja_env.get_template("pond_detail.html")
    html = template.render({
        "request": request,
        "pond_locked": _lock is not None,
        "lock_info": lock_info,
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
        "retagged_unresolved_count": retagged_unresolved_count,
        "net_fish_count": net_fish_count,
        "female_state_4_count": females_state_4_count,
        "caviar_estimated_kg": round(caviar_estimated_kg, 1),
        "caviar_factor_pct": round(caviar_factor * 100.0, 1),
        "caviar_yield_window": caviar_yield.get("window_days"),
        "caviar_yield_stale": bool(caviar_yield.get("stale")),
        "fish_by_lot": fish_by_lot,
        "sex_count_f": sex_count_f,
        "sex_count_m": sex_count_m,
        "sex_count_u": sex_count_u,
        "females_by_state": females_by_state,
        "can_register_from_untagged": unregistered_count > 0,
        "unregistered_lot_options": unregistered_lot_options,
        "tag_detachment_lot_options": tag_detachment_lot_options,
        "pending_detachment_events": [
            {"id": e.id, "event_date": e.event_date, "notes": e.notes, "status": e.status}
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


def _canonical_drug_locked_pit_tag(value: Optional[str]) -> str:
    """Normaliza PIT tag al formato bloqueado por fármaco: sufijo final _R."""
    normalized = _normalize_pit_tag(value)
    if not normalized:
        return ""

    # Corrige casos históricos como _R25, _R24_R25 y _R25_R hacia _R.
    collapsed = re.sub(r"(?:_R\d{1,2}){1,2}(?:_R)?$", "_R", normalized)
    if collapsed.endswith("_R"):
        return collapsed
    return f"{normalized}_R"


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


def _sync_postgres_pk_sequence(model, db: Session) -> None:
    """Realigns a PostgreSQL serial/bigserial sequence with the current max(id)."""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return

    max_id = db.query(func.max(model.id)).scalar() or 0
    db.execute(
        text(
            "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), :next_value, false)"
        ),
        {"table_name": model.__tablename__, "next_value": max_id + 1},
    )


def _create_fish_with_sequence_retry(db: Session, **fish_kwargs) -> Fish:
    """Retries once if the fish primary-key sequence is behind the table max(id)."""
    fish = Fish(**fish_kwargs)
    db.add(fish)
    try:
        db.flush()
        return fish
    except IntegrityError as exc:
        db.rollback()
        if "fish_pkey" not in str(getattr(exc, "orig", exc)):
            raise

        _sync_postgres_pk_sequence(Fish, db)
        fish = Fish(**fish_kwargs)
        db.add(fish)
        db.flush()
        return fish


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


@dataclass
class FishSaveResult:
    ok: bool
    status: str   # "ok" | "error"
    message: str


def apply_fish_save(
    db: Session,
    pond_id: int,
    fish_id: int,
    sex: Optional[str] = None,
    weight: Optional[str] = None,
    diameter: Optional[str] = None,
    development_state: Optional[str] = None,
    move_to: Optional[str] = None,
    action: str = "save",
) -> "FishSaveResult":
    """Lógica canónica de sexado por pez (clasificación + movimiento).

    Fuente única de verdad: la usa el formulario online y la usará la
    sincronización de sexado offline. No hace HTTP; devuelve FishSaveResult.
    """
    def go(status_value: str, message: str) -> "FishSaveResult":
        return FishSaveResult(ok=(status_value == "ok"), status=status_value, message=message)

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
    # Permite: cambios en sex/weight/diameter/development_state/destination O
    # registrar nuevo muestreo aunque valores sean iguales (si user ingresó weight o diameter)
    has_sampling_data = new_weight is not None or new_diameter is not None
    if not (sex_changed or weight_changed or diameter_changed or development_state_changed or destination or has_sampling_data):
        return go("error", "No hay cambios para guardar.")

    now = datetime.utcnow()

    try:
        if sex_changed:
            fish.sex = target_sex

        # Crear nuevo muestreo si: hay cambios en datos de muestreo O user proporciona datos de muestreo (incluso si iguales)
        if sex_changed or weight_changed or diameter_changed or development_state_changed or has_sampling_data:
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

        # Ajuste incremental de biomasa cuando cambia el peso y el pez no se mueve.
        # Si hay movimiento, _adjust_biomass_on_movement ya descontó/sumó el peso
        # usando el nuevo FishSampling (visible por autoflush).
        if weight_changed and not moved and new_weight is not None and fish.lot_id:
            _adjust_biomass_on_individual_sampling(
                pond_id=pond_id,
                lot_id=fish.lot_id,
                new_weight_g=float(new_weight),
                db=db,
            )

        db.commit()

        if moved:
            return go("ok", f"Cambios guardados y pez trasladado a {destination.name}.")
        return go("ok", "Cambios guardados correctamente.")
    except Exception:
        db.rollback()
        return go("error", "No se pudieron guardar los cambios.")


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
    from app.services.sexado_sessions import pond_lock_session
    if pond_lock_session(db, pond_id):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status=error&msg={quote_plus('Estanque en sesión de sexado offline (solo lectura hasta sincronizar).')}",
            status_code=303,
        )
    res = apply_fish_save(
        db, pond_id, fish_id, sex=sex, weight=weight, diameter=diameter,
        development_state=development_state, move_to=move_to, action=action,
    )
    return RedirectResponse(
        url=f"/views/ui/ponds/{pond_id}?status={quote_plus(res.status)}&msg={quote_plus(res.message)}",
        status_code=303,
    )


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

    from app.services.sexado_sessions import pond_lock_session
    if pond_lock_session(db, pond_id):
        return go("error", "Estanque en sesión de sexado offline (solo lectura hasta sincronizar).")

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
    development_state: Optional[str] = Form(None),
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

    pending_event = (
        db.query(TagDetachmentEvent)
        .filter(
            TagDetachmentEvent.pond_id == pond_id,
            TagDetachmentEvent.status == "unidentified",
        )
        .order_by(TagDetachmentEvent.event_date.asc(), TagDetachmentEvent.id.asc())
        .first()
    )
    if pending_event:
        return RedirectResponse(
            url=(
                f"/views/ui/ponds/{pond_id}/retag/{pending_event.id}"
                f"?status={quote_plus('warn')}&msg={quote_plus('Hay un tag perdido vigente. Complete el re-tag para cerrar el caso.')}"
            ),
            status_code=303,
        )

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
    target_development_state = _normalize_development_state(development_state)

    try:
        new_weight = _parse_decimal_field(weight, "Peso")
        new_diameter = _parse_decimal_field(diameter, "Diámetro")
    except ValueError as exc:
        return go("error", str(exc))

    if new_weight is not None and new_weight <= 0:
        return go("error", "El peso debe ser mayor a 0.")
    if new_diameter is not None and new_diameter <= 0:
        return go("error", "El diámetro debe ser mayor a 0.")

    if target_development_state and not target_sex:
        return go("error", "Para actualizar estado de desarrollo debe indicar sexo (hembra o macho).")
    if not _is_valid_development_state_for_sex(target_development_state, target_sex):
        if target_sex == "f":
            return go("error", "Estado de desarrollo inválido para hembra. Use: 0, 1, 2, 3, 4 o R.")
        if target_sex == "m":
            return go("error", "Estado de desarrollo inválido para macho. Use: 0 o L.")
        return go("error", "Estado de desarrollo inválido.")

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

        if new_weight is not None or new_diameter is not None or target_development_state is not None:
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


@router.get("/ui/fish/check-pit-tag")
def fish_check_pit_tag(tag: str, db: Session = Depends(get_db)):
    """AJAX: valida si un PIT tag puede registrarse (libre, bloqueado, o reutilizable)."""
    pit_tag = _normalize_pit_tag(tag)
    if not pit_tag:
        return JSONResponse({"status": "blocked", "message": "Tag vacio."})
    return JSONResponse(_check_pit_tag_reuse(pit_tag, db))


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
    current_pond_id = last_mv.destiny_pond_id if last_mv and last_mv.destiny_pond_id else None
    current_pond_name = pond_name_map.get(current_pond_id) if current_pond_id else None
    can_use_pond_actions = fish.state in ("alive", "depuration") and current_pond_id is not None

    move_pond_options = []
    if current_pond_id is not None:
        move_pond_options = [
            {"id": p.id, "name": p.name}
            for p in db.query(Pond).filter(Pond.state != "inactive").order_by(Pond.name.asc()).all()
            if p.id != current_pond_id
        ]

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

    repro_sel = _repro.active_selection_for(db, fish_id)
    repro_sex_char = (fish.sex or "").strip().upper()[:1]
    repro_ctx = {
        "active": repro_sel is not None,
        "selection_id": repro_sel.id if repro_sel else None,
        "needs_sex": repro_sex_char not in ("F", "M"),
        "sex_char": repro_sex_char if repro_sex_char in ("F", "M") else "",
        "can_select": fish.state == "alive",
    }

    template = jinja_env.get_template("fish_history.html")
    html = template.render({
        "request": request,
        "status": status,
        "msg": msg,
        "repro": repro_ctx,
        "fish": {
            "id": fish.id,
            "internal_id": fish.internal_id,
            "secondary_internal_id": fish.secondary_internal_id or "",
            "notes": fish.notes or "",
            "sex": fish.sex,
            "sex_display": sex_display,
            "state": fish.state,
            "lot": (lot.name if lot and lot.name else (lot.internal_id if lot else "N/D")),
            "lot_name": lot.name if lot else None,
            "can_edit_internal_id": not (has_drug_use or has_r_suffix),
            "needs_drug_log": needs_drug_log,
        },
        "current_pond": current_pond_name,
        "current_pond_id": current_pond_id,
        "can_use_pond_actions": can_use_pond_actions,
        "move_pond_options": move_pond_options,
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

    candidate_internal_id = _canonical_drug_locked_pit_tag(normalized_current)
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
        "sex": _normalize_sex_value(fish.sex),
        "lot": (lot.name or lot.internal_id) if lot else "N/D",
        "lot_id": fish.lot_id,
        "current_pond": current_pond,
    })


@router.get("/ui/ponds/{pond_id}/pit-tags")
def ui_pond_pit_tags(pond_id: int, db: Session = Depends(get_db)):
    """Retorna PIT tags actuales del estanque para autocompletar y carga masiva en UI."""
    fish_list = _get_current_tagged_fish_in_pond(pond_id, db)
    pit_tags = [f.internal_id for f in fish_list if f.internal_id]
    fish = [
        {"id": f.id, "pit": f.internal_id, "sex": _normalize_sex_value(f.sex)}
        for f in fish_list
        if f.internal_id
    ]
    return JSONResponse({"pit_tags": pit_tags, "fish": fish})


# ── Formulario de nuevo movimiento ─────────────────────────────────────────
@router.get("/ui/movements/new", response_class=HTMLResponse)
def ui_movement_new(
    request: Request,
    source_pond_id: Optional[int] = None,
    mode: Optional[str] = None,
    tagged_batch: bool = False,
    db: Session = Depends(get_db),
):
    ponds = db.query(Pond).filter(Pond.state != "inactive").order_by(Pond.name).all()
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
    "reconciliation", "faena",
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
    batch_development_state: Optional[str] = Form(None),
    lot_id: Optional[str] = Form(None),
    fish_quantity_tagged: int = Form(1),
    fish_quantity_untagged: Optional[int] = Form(None),
    post_action: str = Form("go_source"),
    db: Session = Depends(get_db),
):
    ponds = db.query(Pond).filter(Pond.state != "inactive").order_by(Pond.name).all()
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

    # Faena solo para peces sin marca desde este formulario. Los peces con PIT tag
    # se envían a faena desde el estanque de depuración (requiere sexo/peso/diámetro).
    if movement_reason == "faena":
        if mode != "untagged":
            return render_form(
                error="La faena de peces con PIT tag se registra desde el estanque de "
                      "depuración, no desde este formulario."
            )
        if dst_id:
            return render_form(error="La faena es un egreso: no debe tener estanque destino.")
        if not src_id:
            return render_form(error="Debe indicar el estanque origen para enviar peces a faena.")

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

                # Rechazar movimiento backdateado: si el timestamp declarado es anterior
                # al último movimiento real del pez, la asignación de origen es incoherente.
                if last_movement.movement_time and mov_time < last_movement.movement_time:
                    failed_pits.append(
                        f"{pit} (fecha {mov_time.strftime('%d/%m %H:%M')} anterior al "
                        f"último movimiento {last_movement.movement_time.strftime('%d/%m %H:%M')})"
                    )
                    continue

                movable_fish.append(fish)

            if not movable_fish:
                detail = "; ".join(failed_pits[:20])
                if len(failed_pits) > 20:
                    detail += f" ... (+{len(failed_pits)-20})"
                return render_form(error=f"No se movieron peces con PIT. Fallidos: {detail}")

            # Estado de desarrollo masivo (opcional): se registra un muestreo por
            # cada pez efectivamente movido. Solo es válido si todos los peces
            # movibles comparten el mismo sexo registrado (autoritativo en servidor).
            batch_dev_state = _normalize_development_state(batch_development_state)
            if batch_dev_state:
                sexes = {_normalize_sex_value(f.sex) for f in movable_fish}
                if None in sexes or len(sexes) != 1:
                    return render_form(
                        error="Para registrar estado de desarrollo masivo, todos los peces "
                              "movidos deben tener el mismo sexo registrado."
                    )
                common_sex = next(iter(sexes))
                if not _is_valid_development_state_for_sex(batch_dev_state, common_sex):
                    if common_sex == "f":
                        return render_form(error="Estado de desarrollo inválido para hembra. Use: 0, 1, 2, 3, 4 o R.")
                    if common_sex == "m":
                        return render_form(error="Estado de desarrollo inválido para macho. Use: 0 o L.")
                    return render_form(error="Estado de desarrollo inválido.")

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

                    # Registro masivo de estado de desarrollo (nuevo muestreo por pez).
                    if batch_dev_state:
                        db.add(FishSampling(
                            fish_id=fish.id,
                            development_state=batch_dev_state,
                            registry_time=mov_time,
                            created_at=datetime.utcnow(),
                            updated_at=datetime.utcnow(),
                        ))

                    moved_pits.append(fish.internal_id or str(fish.id))

                _refresh_pond_runtime_cache_many([src_id, dst_id], db)
                # Recalculate avg_weight after movements
                if src_id:
                    _recalc_pond_biomass(src_id, db)
                if dst_id:
                    _recalc_pond_biomass(dst_id, db)
                db.commit()
            except Exception:
                db.rollback()
                return render_form(error="No se pudo registrar el movimiento masivo con PIT tag.")

            dest_label = dest_pond.name if dst_id and dest_pond else "egreso"
            msg = f"Peces movidos con éxito: {len(moved_pits)} hacia {dest_label}."
            if batch_dev_state:
                msg += f" Estado de desarrollo '{batch_dev_state}' registrado en {len(moved_pits)} pez(es)."
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

        try:
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
            # Recalculate avg_weight after movements
            if src_id:
                _recalc_pond_biomass(src_id, db)
            if dst_id:
                _recalc_pond_biomass(dst_id, db)
            db.commit()
            dest_name = dest_pond.name if dst_id and dest_pond else "egreso"
            fish_label = fish.internal_id or str(fish.id)
            return go_after_save("ok", f"Peces movidos con éxito: 1 ({fish_label}) hacia {dest_name}.")
        except Exception:
            db.rollback()
            return render_form(error="No se pudo registrar el movimiento del pez con PIT tag.")

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

        try:
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
            # Recalculate avg_weight after movements
            if src_id:
                _recalc_pond_biomass(src_id, db)
            if dst_id:
                _recalc_pond_biomass(dst_id, db)
            db.commit()

            lot = db.query(Lot).filter(Lot.id == lot_id_int).first()
            lot_label = lot.internal_id if lot else str(lot_id_int)
            if movement_reason == "faena":
                return go_after_save("ok", f"Peces enviados a faena: {fish_quantity_untagged} sin marca (lote {lot_label}).")
            return go_after_save("ok", f"Peces movidos con éxito: {fish_quantity_untagged} sin PIT (lote {lot_label}).")
        except Exception:
            db.rollback()
            return render_form(error="No se pudo registrar el movimiento de peces sin PIT tag.")


# ══════════════════════════════════════════════════════════════════════════════
# MUESTREO RUTINARIO
# ══════════════════════════════════════════════════════════════════════════════

def _get_last_sampling_date(pond_id: int, db: Session):
    """Fecha del último muestreo cerrado para el estanque (registry_date)."""
    row = (
        db.query(SamplingSession.registry_date)
        .filter(SamplingSession.pond_id == pond_id, SamplingSession.closed_at.isnot(None))
        .order_by(SamplingSession.registry_date.desc())
        .first()
    )
    return row[0] if row else None


def _get_fish_weight_estimate(fish_id: int, lot_id: int, db: Session, pond_id: Optional[int] = None) -> Optional[float]:
    """
    Peso estimado de un pez marcado para ajustes de biomasa.

    Criterio híbrido (STALE_WEIGHT_DAYS = 90 días):
      - Si el último peso individual fue tomado hace ≤ 90 días desde el último
        muestreo del estanque (F) → usar peso individual (fresco y preciso).
      - Si no → usar pond_lot_stats.avg_weight del estanque origen.
        Esto garantiza consistencia con _recalc_pond_biomass, que aplica el
        mismo floor para calcular biomass_current.
    """
    row = (
        db.query(FishSampling.weight, FishSampling.registry_time, FishSampling.created_at)
        .filter(FishSampling.fish_id == fish_id, FishSampling.weight.isnot(None))
        .order_by(func.coalesce(FishSampling.registry_time, FishSampling.created_at).desc())
        .first()
    )

    if row:
        individual_weight = float(row[0])
        weight_time = row[1] or row[2]

        if pond_id and weight_time:
            last_F = _get_last_sampling_date(pond_id, db)
            if last_F:
                weight_date = weight_time.date() if isinstance(weight_time, datetime) else weight_time
                days_stale = (last_F - weight_date).days
                if days_stale > STALE_WEIGHT_DAYS:
                    # Peso viejo respecto al último muestreo: usar avg del lote en el estanque
                    lot_avg = _get_lot_weight_estimate(pond_id, lot_id, db)
                    if lot_avg:
                        return lot_avg

        return individual_weight

    # Sin peso individual: intentar avg del estanque, luego avg global del lote
    lot_avg = _get_lot_weight_estimate(pond_id, lot_id, db) if pond_id else None
    if lot_avg:
        return lot_avg
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
    # fallback global del lote — ordenado por fecha de muestreo real
    row2 = (
        db.query(PondLotStats.avg_weight)
        .filter(PondLotStats.lot_id == lot_id, PondLotStats.avg_weight.isnot(None))
        .order_by(PondLotStats.sampled_at.desc().nullslast(), PondLotStats.updated_at.desc().nullslast())
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
    EXIT_REASONS = {"mortality", "devious", "missing_number", "unmarked_devious", "reconciliation"}

    # El vaciado por reconciliación elimina registros duplicados/residuales: descuenta
    # biomasa del estanque (para dejarlo en 0) pero NO de la biomasa del lote, porque
    # los peces reales ya salieron por sus flujos normales (faena/traslado/mortalidad).
    skip_lot_biomass = mov.movement_reason == "reconciliation"

    if mov.fish_id:
        # ── TAGGED ──
        fish = db.query(Fish).filter(Fish.id == mov.fish_id).first()
        lot_id = fish.lot_id if fish else mov.lot_id
        # Criterio híbrido: pasar pond_id para que _get_fish_weight_estimate aplique
        # el floor de 90 días coherente con _recalc_pond_biomass.
        peso_g = _get_fish_weight_estimate(mov.fish_id, lot_id, db, pond_id=mov.source_pond_id)
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
        elif not skip_lot_biomass:
            # Egreso definitivo (mortalidad, faena, etc.) → descuenta biomasa del lote
            if lot_id:
                lot_obj = db.query(Lot).filter(Lot.id == lot_id).first()
                if lot_obj and lot_obj.biomass_current is not None:
                    lot_obj.biomass_current = max(Decimal("0"), Decimal(str(lot_obj.biomass_current)) - delta_kg)

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
        elif not skip_lot_biomass:
            # Egreso definitivo (mortalidad, faena, etc.) → descuenta biomasa del lote
            if mov.lot_id:
                lot_obj = db.query(Lot).filter(Lot.id == mov.lot_id).first()
                if lot_obj and lot_obj.biomass_current is not None:
                    lot_obj.biomass_current = max(Decimal("0"), Decimal(str(lot_obj.biomass_current)) - delta_kg)


def _save_biomass_checkpoint(
    checkpoint_date,
    checkpoint_type: str,
    pond_lot_biomass: dict,
    pond_lot_counts: dict,
    pond_lot_weights: dict,
    source_session_id,
    db: Session,
) -> None:
    now = datetime.utcnow()
    for (pond_id, lot_id), biomass_kg in pond_lot_biomass.items():
        qty = pond_lot_counts.get((pond_id, lot_id), 0)
        if qty <= 0 and biomass_kg <= 0:
            continue
        w_g = pond_lot_weights.get((pond_id, lot_id))
        existing = (
            db.query(BiomassCheckpoint)
            .filter_by(
                checkpoint_date=checkpoint_date,
                checkpoint_type=checkpoint_type,
                lot_id=lot_id,
                pond_id=pond_id,
            )
            .first()
        )
        if existing:
            existing.fish_count   = qty
            existing.biomass_kg   = round(float(biomass_kg), 3)
            existing.avg_weight_g = round(float(w_g), 3) if w_g else None
            existing.computed_at  = now
        else:
            db.add(BiomassCheckpoint(
                checkpoint_date=checkpoint_date,
                checkpoint_type=checkpoint_type,
                lot_id=lot_id,
                pond_id=pond_id,
                fish_count=qty,
                biomass_kg=round(float(biomass_kg), 3),
                avg_weight_g=round(float(w_g), 3) if w_g else None,
                source_session_id=source_session_id,
                computed_at=now,
            ))
    db.flush()


def _read_biomass_checkpoint(
    checkpoint_date,
    checkpoint_type: str,
    db: Session,
) -> tuple[dict, dict]:
    """Retorna (lot_biomass, pond_biomass) desde la tabla de checkpoints.

    lot_biomass:  lot_id  → biomass_kg (suma de todos los estanques del lote)
    pond_biomass: pond_id → biomass_kg (suma de todos los lotes del estanque)
    Retorna ({}, {}) si no existe checkpoint para esa fecha/tipo.
    """
    rows = (
        db.query(BiomassCheckpoint)
        .filter_by(checkpoint_date=checkpoint_date, checkpoint_type=checkpoint_type)
        .all()
    )
    lot_bm:  dict[int, float] = defaultdict(float)
    pond_bm: dict[int, float] = defaultdict(float)
    for row in rows:
        lot_bm[int(row.lot_id)]   += float(row.biomass_kg)
        pond_bm[int(row.pond_id)] += float(row.biomass_kg)
    return dict(lot_bm), dict(pond_bm)


def _get_best_checkpoints_at(
    target_date: "date",
    db: Session,
) -> "dict[tuple[int,int], Any]":
    """Retorna la fila de checkpoint más reciente ≤ target_date por (pond_id, lot_id).

    Desempata: fecha más reciente primero, luego 'sampling' > 'month_start'.
    """
    rows = db.execute(
        text("""
            SELECT DISTINCT ON (pond_id, lot_id)
                id, pond_id, lot_id, checkpoint_date, checkpoint_type,
                biomass_kg, fish_count, avg_weight_g
            FROM biomass_checkpoints
            WHERE checkpoint_date <= :target_date
            ORDER BY pond_id, lot_id,
                     checkpoint_date DESC,
                     CASE checkpoint_type WHEN 'sampling' THEN 0 ELSE 1 END ASC
        """),
        {"target_date": target_date},
    ).fetchall()
    return {(int(r.pond_id), int(r.lot_id)): r for r in rows}


def _get_source_checkpoint_at(
    source_pond_id: int,
    lot_id: int,
    movement_date: "date",
    db: Session,
):
    """Checkpoint más reciente para (source_pond, lot) en fecha ≤ movement_date."""
    return db.execute(
        text("""
            SELECT avg_weight_g, biomass_kg, fish_count, checkpoint_date
            FROM biomass_checkpoints
            WHERE pond_id = :pond_id AND lot_id = :lot_id
              AND checkpoint_date <= :target_date
            ORDER BY checkpoint_date DESC,
                     CASE checkpoint_type WHEN 'sampling' THEN 0 ELSE 1 END ASC
            LIMIT 1
        """),
        {"pond_id": source_pond_id, "lot_id": lot_id, "target_date": movement_date},
    ).fetchone()


def _adjust_biomass_on_individual_sampling(
    pond_id: int,
    lot_id: int,
    new_weight_g: float,
    db: Session,
) -> None:
    """Ajuste incremental de biomasa al registrar peso individual de un pez marcado
    fuera de una sesión de muestreo periódica.

    El pez estaba en el grupo stale: su contribución anterior a pond.biomass_current
    era pond_lot_stats.avg_weight (el promedio del estanque para ese lote). La reemplazamos
    por el nuevo peso individual registrado.
    """
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if pond is None or pond.biomass_current is None:
        return

    stats = (
        db.query(PondLotStats)
        .filter(PondLotStats.pond_id == pond_id, PondLotStats.lot_id == lot_id)
        .first()
    )
    if stats is None or stats.avg_weight is None:
        return

    avg_w_g = float(stats.avg_weight)
    delta_kg = Decimal(str(round(new_weight_g - avg_w_g, 6))) / 1000

    pond.biomass_current = max(Decimal("0"), Decimal(str(pond.biomass_current)) + delta_kg)
    pond.updated_at = datetime.utcnow()

    lot = db.query(Lot).filter(Lot.id == lot_id).first()
    if lot is not None and lot.biomass_current is not None:
        lot.biomass_current = max(Decimal("0"), Decimal(str(lot.biomass_current)) + delta_kg)

    db.flush()


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
    1. Calcula avg_weight, avg_length y K por (pond, lot) usando TODOS los registros
       (marcados + no marcados) — el promedio es más representativo con la muestra completa.
    2. Hace UPSERT en pond_lot_stats con sampled_at = session.registry_date
    3. Recalcula biomasa del estanque
    4. Actualiza lot.biomass_current para todos los lotes afectados
    5. Marca la sesión como cerrada
    """
    now = datetime.utcnow()

    # Todos los registros con lot_id y weight (marcados + no marcados)
    records_all = (
        db.query(SamplingRecord)
        .filter(
            SamplingRecord.session_id == session.id,
            SamplingRecord.lot_id.isnot(None),
            SamplingRecord.weight.isnot(None),
        )
        .all()
    )

    by_lot: dict = defaultdict(list)
    for r in records_all:
        by_lot[r.lot_id].append(r)

    for lot_id, recs in by_lot.items():
        weights = [float(r.weight) for r in recs if r.weight is not None]
        lengths = [float(r.length) for r in recs if r.length is not None]
        n = len(recs)
        avg_w = sum(weights) / len(weights) if weights else None
        avg_l = sum(lengths) / len(lengths) if lengths else None

        # K = (peso_g / (longitud_cm)³) × 100  — longitud almacenada en cm
        k = None
        if avg_w and avg_l and avg_l > 0:
            k = round((avg_w / (avg_l ** 3)) * 100, 4)

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
            existing.sampled_at = session.registry_date
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
                sampled_at=session.registry_date,
                updated_at=now,
            ))

    db.flush()

    # Recalcular biomasa del estanque (mantiene pond.biomass_current como cache)
    _recalc_pond_biomass(session.pond_id, db)

    # Guardar checkpoint 'sampling' — biomass_kg desde mediciones, no desde biomass_current
    pond_lot_counts_now = _sum_pond_lot_balance_until(
        datetime.utcnow() + timedelta(seconds=1), db
    )
    # avg_weight por (estanque, lote) desde los registros de esta sesión
    pond_lot_weights_for_ckpt: dict[tuple[int, int], float] = {}
    for _lot_id, _recs in by_lot.items():
        _weights = [float(r.weight) for r in _recs if r.weight is not None]
        if _weights:
            pond_lot_weights_for_ckpt[(session.pond_id, _lot_id)] = sum(_weights) / len(_weights)

    # biomass_kg = conteo_actual × avg_weight_medido / 1000 (sin biomass_current)
    pond_checkpoint_biomass = {
        (session.pond_id, lot_id): (
            float(pond_lot_counts_now.get((session.pond_id, lot_id), 0)) * avg_w / 1000.0
        )
        for (pond_id, lot_id), avg_w in pond_lot_weights_for_ckpt.items()
    }
    pond_checkpoint_counts = {
        k: v for k, v in pond_lot_counts_now.items()
        if k[0] == session.pond_id
    }
    if pond_checkpoint_biomass:
        _save_biomass_checkpoint(
            checkpoint_date=session.registry_date,
            checkpoint_type="sampling",
            pond_lot_biomass=pond_checkpoint_biomass,
            pond_lot_counts=pond_checkpoint_counts,
            pond_lot_weights=pond_lot_weights_for_ckpt,
            source_session_id=session.id,
            db=db,
        )

    # Marcar sesión como cerrada
    session.closed_at = now
    session.updated_at = now


def _get_sampling_session_context(session: SamplingSession, db: Session) -> dict:
    """Construye el contexto completo de una sesión de muestreo para el template."""
    pond = db.query(Pond).filter(Pond.id == session.pond_id).first()

    # Peces con PIT activos en el estanque para datalist
    tagged_fish = _get_current_tagged_fish_in_pond(session.pond_id, db)
    fish_options = [{"id": f.id, "pit": f.internal_id, "lot_id": f.lot_id} for f in tagged_fish]

    # Lotes disponibles (de peces registrados y no registrados)
    lot_ids_registered = list({f.lot_id for f in tagged_fish if f.lot_id})
    unregistered_balances = _get_unregistered_balances_by_lot(session.pond_id, db)
    lot_ids_unregistered = list(unregistered_balances.keys())
    all_lot_ids = list(set(lot_ids_registered + lot_ids_unregistered))
    lots_map = {
        l.id: l for l in db.query(Lot).filter(Lot.id.in_(all_lot_ids)).all()
    } if all_lot_ids else {}

    # Lote base: único lote con peces sin marcar en este estanque (balance > 0)
    base_lot_id = next((lid for lid, bal in unregistered_balances.items() if bal > 0), None)

    # Mapa de especies para thresholds de validación
    species_ids = list({l.species_id for l in lots_map.values() if l.species_id})
    species_map = {
        s.id: s.name
        for s in db.query(Species).filter(Species.id.in_(species_ids)).all()
    } if species_ids else {}

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
    lot_stats = []
    for s in lot_stats_rows:
        lot_obj = lots_map.get(s.lot_id)
        lot_label = (
            (lot_obj.internal_id or lot_obj.name) if lot_obj else str(s.lot_id)
        )
        species_name = (
            species_map.get(lot_obj.species_id) if lot_obj and lot_obj.species_id else None
        )
        lot_stats.append({
            "lot_id": s.lot_id,
            "lot_label": lot_label,
            "avg_weight": float(s.avg_weight) if s.avg_weight else None,
            "avg_length": float(s.avg_length) if s.avg_length else None,
            "condition_k": float(s.condition_k) if s.condition_k else None,
            "n_sampled": s.n_sampled,
            "species_name": species_name,
        })

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
        "base_lot_id": base_lot_id,
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

    return RedirectResponse(
        url=f"/views/ui/ponds/{pond_id}?status=warn&msg={quote_plus('Usa la tarjeta Pérdida Pit-tag en esta vista para registrar o declarar mortalidad sin tag.')}",
        status_code=303,
    )


@router.post("/ui/ponds/{pond_id}/tag-detachment")
def ui_tag_detachment_save(
    pond_id: int,
    request: Request,
    event_date: str = Form(...),
    lot_id: Optional[str] = Form(None),
    tagloss_quantity: Optional[str] = Form(None),
    action: str = Form("register"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    from datetime import datetime

    def go(s, m):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status={quote_plus(s)}&msg={quote_plus(m)}",
            status_code=303,
        )

    try:
        event_dt = datetime.fromisoformat(event_date)
    except ValueError:
        return go("error", "Fecha inválida.")

    if action not in {"register", "mortality"}:
        return go("error", "Acción no válida.")

    raw_qty = (tagloss_quantity or "").strip()
    if not raw_qty:
        return go("error", "Debe ingresar una cantidad para la pérdida de tag.")
    try:
        quantity = int(raw_qty)
    except ValueError:
        return go("error", "La cantidad debe ser un número entero.")

    if quantity < 1:
        return go("error", "La cantidad debe ser mayor a 0.")

    selected_lot_id: Optional[int] = None
    if lot_id and lot_id.strip():
        if not lot_id.strip().isdigit():
            return go("error", "Lote no válido.")
        selected_lot_id = int(lot_id.strip())

    if selected_lot_id is None:
        # Fallback: primer lote disponible en la laguna.
        available_lot_ids = _get_pond_available_lot_ids(pond_id, db)
        selected_lot_id = available_lot_ids[0] if available_lot_ids else None

    if selected_lot_id is None:
        return go("error", "No se pudo determinar lote. Selecciona un lote para continuar.")

    lot_obj = db.query(Lot).filter(Lot.id == selected_lot_id).first()
    if not lot_obj:
        return go("error", "El lote seleccionado no existe.")

    lot_label = lot_obj.internal_id if lot_obj.internal_id else lot_obj.name
    note_base = notes.strip() if notes and notes.strip() else None
    note_with_lot = f"[lote:{lot_label}] {note_base}" if note_base else f"[lote:{lot_label}]"

    now = datetime.utcnow()
    try:
        if action == "register":
            events = [
                TagDetachmentEvent(
                    pond_id=pond_id,
                    fish_id=None,
                    event_date=event_dt,
                    notes=note_with_lot,
                    status="unidentified",
                )
                for _ in range(quantity)
            ]
            db.add_all(events)

            in_unregistered = PondMovement(
                fish_id=None,
                lot_id=selected_lot_id,
                source_pond_id=None,
                destiny_pond_id=pond_id,
                fish_quantity=quantity,
                movement_reason="inventory_mismatch",
                movement_time=event_dt,
                created_at=now,
                updated_at=now,
            )
            db.add(in_unregistered)
            db.flush()
            _adjust_biomass_on_movement(in_unregistered, db)
            _refresh_pond_runtime_cache_many([pond_id], db)
            db.commit()
            return go(
                "ok",
                f"Pérdida de tag registrada en lote {lot_label}: {quantity} evento(s) creado(s) y {quantity} pez(ces) sin tag agregado(s) al estanque.",
            )

        # action == mortality
        events = [
            TagDetachmentEvent(
                pond_id=pond_id,
                fish_id=None,
                event_date=event_dt,
                notes=note_with_lot,
                status="written_off",
                resolution="mortality",
                resolved_at=event_dt,
            )
            for _ in range(quantity)
        ]
        db.add_all(events)

        balances = _get_unregistered_balances_by_lot(pond_id, db)
        available_balance = int(balances.get(selected_lot_id, 0) or 0)
        shortfall = max(0, quantity - available_balance)
        if shortfall > 0:
            # Si no alcanza el saldo sin tag en el lote, registramos ingreso para cubrir el faltante
            # y luego mortalidad del total solicitado.
            in_unregistered = PondMovement(
                fish_id=None,
                lot_id=selected_lot_id,
                source_pond_id=None,
                destiny_pond_id=pond_id,
                fish_quantity=shortfall,
                movement_reason="inventory_mismatch",
                movement_time=event_dt,
                created_at=now,
                updated_at=now,
            )
            db.add(in_unregistered)
            db.flush()
            _adjust_biomass_on_movement(in_unregistered, db)

        out_mortality = PondMovement(
            fish_id=None,
            lot_id=selected_lot_id,
            source_pond_id=pond_id,
            destiny_pond_id=None,
            fish_quantity=quantity,
            movement_reason="mortality",
            movement_time=event_dt,
            created_at=now,
            updated_at=now,
        )
        db.add(out_mortality)
        db.flush()
        _adjust_biomass_on_movement(out_mortality, db)
        _refresh_pond_runtime_cache_many([pond_id], db)
        db.commit()
        return go(
            "ok",
            f"Mortalidad sin tag registrada para lote {lot_label}: {quantity} evento(s) cerrado(s).",
        )
    except Exception:
        db.rollback()
        return go("error", "No se pudo registrar el evento de pérdida/mortalidad sin tag.")


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

    # Fallback: si no hay lotes sin tag, permitir lotes disponibles en la laguna.
    if not lot_options:
        present_lot_ids = sorted(
            {
                int(f.lot_id)
                for f in _get_current_tagged_fish_in_pond(pond_id, db)
                if f.lot_id is not None
            }
            | set(unregistered_balances.keys())
        )
        if present_lot_ids:
            present_lots = db.query(Lot).filter(Lot.id.in_(present_lot_ids)).all()
            lot_options = [
                {
                    "id": lot.id,
                    "label": lot.internal_id or lot.name,
                    "qty": 0,
                    "fallback": True,
                }
                for lot in present_lots
            ]

    template = jinja_env.get_template("retag_form.html")
    female_destination_options = [
        {"id": p.id, "name": p.name}
        for p in db.query(Pond).filter(Pond.id != pond_id, Pond.state != "inactive").order_by(Pond.name.asc(), Pond.id.asc()).all()
    ]
    html = template.render({
        "request": request,
        "pond": pond,
        "event": {"id": event.id, "event_date": event.event_date, "notes": event.notes},
        "lot_options": lot_options,
        "single_lot": lot_options[0] if len(lot_options) == 1 else None,
        "female_destination_options": female_destination_options,
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
    diameter: Optional[str] = Form(None),
    development_state: Optional[str] = Form(None),
    female_destination_pond_id: Optional[str] = Form(None),
    confirm_reuse: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    event = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.id == event_id,
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status.in_(["unidentified", "retagged"]),
        TagDetachmentEvent.resolved_at.is_(None),
    ).first()

    def go(s, m):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status={s}&msg={quote_plus(m)}",
            status_code=303,
        )

    if not pond:
        return go("error", "Estanque no encontrado.")

    if not event:
        return go("error", "Evento no encontrado o ya fue reconciliado. Regresa al detalle del estanque.")

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
    has_unregistered_balance = unregistered_balances.get(selected_lot_id, 0) >= 1

    try:
        new_weight = _parse_decimal_field(weight, "Peso")
        new_diameter = _parse_decimal_field(diameter, "Diámetro")
    except ValueError as exc:
        return go("error", str(exc))

    target_sex = _normalize_sex_value(sex)
    target_development_state = _normalize_development_state(development_state)
    if new_weight is not None and new_weight <= 0:
        return go("error", "El peso debe ser mayor a 0.")
    if new_diameter is not None and new_diameter <= 0:
        return go("error", "El diámetro debe ser mayor a 0.")
    if target_development_state and not target_sex:
        return go("error", "Para actualizar estado de desarrollo debe indicar sexo (hembra o macho).")
    if not _is_valid_development_state_for_sex(target_development_state, target_sex):
        if target_sex == "f":
            return go("error", "Estado de desarrollo inválido para hembra. Use: 0, 1, 2, 3, 4 o R.")
        if target_sex == "m":
            return go("error", "Estado de desarrollo inválido para macho. Use: 0 o L.")
        return go("error", "Estado de desarrollo inválido.")

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
    fish_kwargs = {
        "internal_id": pit_tag,
        "lot_id": selected_lot_id,
        "sex": target_sex,
        "state": "depuration" if pond.depuration else "alive",
        "registration_time": now,
        "depuration_start_time": now if pond.depuration else None,
        "created_at": now,
        "updated_at": now,
    }

    try:
        # Crear nuevo pez con el nuevo tag
        fish = _create_fish_with_sequence_retry(db, **fish_kwargs)

        if not has_unregistered_balance:
            # Fallback legacy: crear ingreso sin-tag para poder consumirlo en el re-tag.
            in_unregistered = PondMovement(
                fish_id=None,
                lot_id=selected_lot_id,
                source_pond_id=None,
                destiny_pond_id=pond_id,
                fish_quantity=1,
                movement_reason="inventory_mismatch",
                movement_time=now,
                created_at=now,
                updated_at=now,
            )
            db.add(in_unregistered)
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

        if new_weight is not None or new_diameter is not None or target_development_state is not None:
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

        moved_female = False
        female_movement = None
        if target_sex == "f" and female_destination:
            female_movement = PondMovement(
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
            db.add(female_movement)
            moved_female = True

            # Transición de estado por depuración para el destino final de la hembra.
            if female_destination.depuration and not pond.depuration:
                fish.depuration_start_time = now
            if female_destination.depuration:
                fish.state = "depuration"
            elif pond.depuration and fish.state == "depuration":
                fish.state = "alive"
                fish.depuration_start_time = None

        # Cerrar el evento de pérdida de tag
        # IMPORTANTE: refrescar event para que SQLAlchemy lo rastree correctamente después de múltiples operaciones
        db.refresh(event)
        event.retag_fish_id = fish.id
        event.status = "retagged"

        if not has_unregistered_balance:
            _adjust_biomass_on_movement(in_unregistered, db)
        _adjust_biomass_on_movement(out_unregistered, db)
        _adjust_biomass_on_movement(in_registered, db)
        affected_pond_ids = {pond_id}
        if moved_female and female_movement is not None:
            _adjust_biomass_on_movement(female_movement, db)
            affected_pond_ids.add(female_destination.id)
        _refresh_pond_runtime_cache_many(affected_pond_ids, db)

        db.commit()
        if moved_female:
            return go("ok", f"PIT tag {pit_tag} asignado y enviado a {female_destination.name}. Pez re-taggeado correctamente.")
        return go("ok", f"PIT tag {pit_tag} asignado. Pez re-taggeado correctamente.")
    except Exception as e:
        db.rollback()
        import traceback
        print(f"Error en re-tag: {str(e)}")
        print(traceback.format_exc())
        return go("error", f"No se pudo registrar el re-tag: {str(e)}")


# ---------------------------------------------------------------------------
# Reconciliación de tags perdidos al cierre del estanque (fase 3)
# ---------------------------------------------------------------------------

def _empty_pond_by_reconciliation(pond_id: int, db: Session, now: datetime) -> dict:
    """
    Vacía el estanque al cierre: crea movimientos de salida (destino NULL, motivo
    'reconciliation') para TODOS los peces que quedan en el registro y cierra los
    eventos de pérdida de tag pendientes. Deja el estanque en 0.

    Premisa (decisión de negocio): los peces reales ya salieron por sus flujos
    normales (faena/traslado/mortalidad). Lo que queda en el registro es residuo
    contable: huérfanos de re-tag + errores de inventario. Por eso se egresan con
    estado terminal no-mortalidad ('reconciled') y sin descontar biomasa de lote.

    Devuelve un resumen: {tagged_out, untagged_out, events_closed, surplus_audited}.
    """
    pending = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status.in_(["unidentified", "retagged"]),
        TagDetachmentEvent.resolved_at.is_(None),
    ).all()

    tagged_fish = _get_current_tagged_fish_in_pond(pond_id, db)
    unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)

    retagged_count = len([e for e in pending if e.status == "retagged"])
    tagged_total = len(tagged_fish)

    # 1) Salida de cada pez taggeado (destino NULL, motivo reconciliation)
    movements: list[PondMovement] = []
    for fish in tagged_fish:
        mov = PondMovement(
            fish_id=fish.id,
            lot_id=fish.lot_id,
            source_pond_id=pond_id,
            destiny_pond_id=None,
            fish_quantity=1,
            movement_reason="reconciliation",
            movement_time=now,
            created_at=now,
            updated_at=now,
        )
        db.add(mov)
        movements.append(mov)
        fish.state = "reconciled"
        fish.devious_time = now
        fish.updated_at = now

    # 2) Vaciar el pool sin tag por lote
    untagged_out = 0
    for lot_id, balance in unregistered_balances.items():
        if balance <= 0:
            continue
        mov = PondMovement(
            fish_id=None,
            lot_id=lot_id,
            source_pond_id=pond_id,
            destiny_pond_id=None,
            fish_quantity=balance,
            movement_reason="reconciliation",
            movement_time=now,
            created_at=now,
            updated_at=now,
        )
        db.add(mov)
        movements.append(mov)
        untagged_out += balance

    # 3) Cerrar eventos pendientes con su resolución
    for event in pending:
        event.status = "written_off"
        event.resolved_at = now
        event.resolution = (
            "retagged_and_transferred" if event.status == "retagged"
            else "left_unregistered"
        )

    # 4) Auditar excedente (Caso B): peces taggeados que exceden a los eventos
    #    retagged → dejar traza como ajuste de inventario.
    surplus = max(0, tagged_total - retagged_count)
    surplus_audited = 0
    if surplus > 0:
        for _ in range(surplus):
            db.add(TagDetachmentEvent(
                pond_id=pond_id,
                fish_id=None,
                event_date=now,
                notes="[Ajuste de inventario por vaciado de estanque]",
                status="written_off",
                resolution="other",
                resolved_at=now,
            ))
        surplus_audited = surplus

    # 5) Marca temporal de reconciliación
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if pond is not None:
        pond.last_tag_reconciliation_at = now

    # 6) Ajustar biomasa (no descuenta biomasa de lote por motivo reconciliation)
    db.flush()
    for mov in movements:
        _adjust_biomass_on_movement(mov, db)

    # 7) Refrescar cache → tagged=0, unregistered=0, n_fish_cached=0
    _refresh_pond_runtime_cache(pond_id, db)

    return {
        "tagged_out": tagged_total,
        "untagged_out": untagged_out,
        "events_closed": len(pending),
        "surplus_audited": surplus_audited,
    }


@router.get("/ui/ponds/{pond_id}/tag-reconciliation", response_class=HTMLResponse)
def ui_tag_reconciliation_form(
    pond_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    # Eventos pendientes de reconciliación: unidentified O retagged sin resolver aún
    pending = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status.in_(["unidentified", "retagged"]),
        TagDetachmentEvent.resolved_at.is_(None),
    ).order_by(TagDetachmentEvent.event_date.asc()).all()

    # Eventos ya resueltos (para mostrar como información)
    resolved = db.query(TagDetachmentEvent).filter(
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status == "written_off",
        TagDetachmentEvent.resolved_at.isnot(None),
    ).all()

    # Validación: usar caché de estanque (es correcto, refleja peces actuales)
    unregistered_balances = _get_unregistered_balances_by_lot(pond_id, db)
    unregistered_total = sum(unregistered_balances.values())
    tagged_total = len(_get_current_tagged_fish_in_pond(pond_id, db))
    current_fish_count = tagged_total + unregistered_total

    events_retagged_count = len([e for e in pending if e.status == "retagged"])
    numbers_match = events_retagged_count == current_fish_count
    allow_bulk_reconciliation = numbers_match

    # Separar eventos: sin resolver primero, luego re-tagueados
    events_unidentified = [e for e in pending if e.status == "unidentified"]
    events_retagged = [e for e in pending if e.status == "retagged"]

    events_data_unidentified = [
        {
            "id": e.id,
            "event_date": e.event_date,
            "notes": e.notes or "",
            "status": e.status,
        }
        for e in events_unidentified
    ]

    events_data_retagged = [
        {
            "id": e.id,
            "event_date": e.event_date,
            "notes": e.notes or "",
            "status": e.status,
        }
        for e in events_retagged
    ]

    # Si no hay eventos pendientes pero sí hay eventos resueltos, mostrar un estado de finalizado
    if not pending and resolved:
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status=ok&msg=%E2%9C%93+Todos+los+eventos+de+p%C3%A9rdida+de+tag+han+sido+reconciliados.",
            status_code=303,
        )

    # Si no hay eventos pendientes ni resueltos, no hay nada que hacer
    if not pending and not resolved:
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status=ok&msg=No+hay+eventos+pendientes+de+reconciliaci%C3%B3n.",
            status_code=303,
        )

    # Obtener lotes disponibles (los que tienen peces en esta laguna)
    current_fish = _get_current_tagged_fish_in_pond(pond_id, db)
    lot_ids = set(f.lot_id for f in current_fish if f.lot_id)
    lots = db.query(Lot).filter(Lot.id.in_(lot_ids)).all() if lot_ids else []
    lot_options = [
        {"id": lot.id, "label": lot.internal_id or lot.name}
        for lot in sorted(lots, key=lambda l: l.internal_id or l.name)
    ]

    template = jinja_env.get_template("tag_reconciliation_form.html")
    html = template.render({
        "request": request,
        "pond": pond,
        "events_unidentified": events_data_unidentified,
        "events_retagged": events_data_retagged,
        "events_retagged_count": events_retagged_count,
        "current_fish_count": current_fish_count,
        "tagged_total": tagged_total,
        "unregistered_total": unregistered_total,
        "numbers_match": numbers_match,
        "allow_bulk_reconciliation": allow_bulk_reconciliation,
        "resolved_count": len(resolved),
        "lot_options": lot_options,
    })
    return HTMLResponse(content=html)


def _do_empty_pond(pond_id: int, db: Session):
    """Ejecuta el vaciado del estanque y redirige al detalle con un resumen."""
    def go(s, m):
        return RedirectResponse(
            url=f"/views/ui/ponds/{pond_id}?status={s}&msg={quote_plus(m)}",
            status_code=303,
        )

    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="Pond not found")

    # Guarda: si no hay nada pendiente ni peces, no-op
    pending_count = db.query(func.count(TagDetachmentEvent.id)).filter(
        TagDetachmentEvent.pond_id == pond_id,
        TagDetachmentEvent.status.in_(["unidentified", "retagged"]),
        TagDetachmentEvent.resolved_at.is_(None),
    ).scalar() or 0
    tagged_present = len(_get_current_tagged_fish_in_pond(pond_id, db))
    untagged_present = sum(_get_unregistered_balances_by_lot(pond_id, db).values())

    if pending_count == 0 and tagged_present == 0 and untagged_present == 0:
        return go("ok", "El estanque ya está vacío. No había nada que reconciliar.")

    try:
        now = datetime.utcnow()
        summary = _empty_pond_by_reconciliation(pond_id, db, now)
        db.commit()
        return go(
            "ok",
            f"✓ Estanque vaciado: {summary['tagged_out']} pez(ces) taggeado(s) y "
            f"{summary['untagged_out']} sin tag egresados por reconciliación; "
            f"{summary['events_closed']} evento(s) cerrado(s). El estanque quedó en 0.",
        )
    except Exception as e:
        db.rollback()
        import traceback
        print(f"Error al vaciar estanque {pond_id}: {e}")
        print(traceback.format_exc())
        return go("error", f"No se pudo vaciar el estanque: {e}")


@router.post("/ui/ponds/{pond_id}/tag-reconciliation/bulk")
async def ui_tag_reconciliation_bulk_save(
    pond_id: int,
    db: Session = Depends(get_db),
):
    """Vaciado masivo (cuadre perfecto): egresa todo y deja el estanque en 0."""
    return _do_empty_pond(pond_id, db)


@router.post("/ui/ponds/{pond_id}/tag-reconciliation")
async def ui_tag_reconciliation_save(
    pond_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Vaciado del estanque: egresa todos los peces restantes (taggeados + sin tag)
    con motivo 'reconciliation', cierra los eventos pendientes y deja el estanque en 0."""
    return _do_empty_pond(pond_id, db)


# ---------------------------------------------------------------------------
# Vista general de lotes
# ---------------------------------------------------------------------------

def _build_lot_summaries(db: Session) -> list[dict]:
    """
    Construye el resumen de todos los lotes activos (con al menos 1 pez vivo).
    Usa queries agregados — sin N+1.
    """
    from datetime import date as _date
    _proj, _ = _project_biomass_from_checkpoint(_date.today(), db)
    lot_biomass_map: dict[int, float] = defaultdict(float)
    for (_pid, _lid), _bm in _proj.items():
        lot_biomass_map[_lid] += _bm

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


def _project_biomass_from_checkpoint(
    target_date: "date",
    db: Session,
) -> "tuple[dict[tuple[int,int], float], list[str]]":
    """Retorna ({(pond_id, lot_id): biomass_kg}, alertas) proyectado a target_date.

    Algoritmo (modelo 3 capas, capas 1 y 2):
    - Ancla: mejor checkpoint ≤ target_date por (pond, lot)
    - Salidas post-ckpt: qty × chk.avg_weight_g del estanque actual
    - Llegadas post-ckpt: qty × ckpt_fuente.avg_weight_g del estanque fuente
    - Sin checkpoint → alerta, biomasa 0
    """
    from datetime import time as _dtime
    target_dt = datetime.combine(target_date, _dtime(23, 59, 59))

    counts_at_target = _sum_pond_lot_balance_until(target_dt, db)
    active = {k: v for k, v in counts_at_target.items() if v > 0}
    if not active:
        return {}, []

    checkpoints = _get_best_checkpoints_at(target_date, db)
    active_pond_ids = list({pid for (pid, _) in active})
    active_lot_ids  = list({lid for (_, lid) in active})

    # Pre-cargar todos los checkpoints para lookups en memoria (elimina N+1 queries).
    # Ordenados ASC por fecha; para misma fecha, sampling > month_start (queda último → bisect lo prefiere).
    all_chk_rows = db.execute(
        text("""
            SELECT pond_id, lot_id, checkpoint_date, avg_weight_g
            FROM biomass_checkpoints
            ORDER BY pond_id, lot_id,
                     checkpoint_date ASC,
                     CASE checkpoint_type WHEN 'sampling' THEN 1 ELSE 0 END ASC
        """),
    ).fetchall()

    # {(pond_id, lot_id): (sorted_dates_list, rows_list)} — índices paralelos para bisect
    _chk_dates:  dict[tuple[int,int], list] = defaultdict(list)
    _chk_rows:   dict[tuple[int,int], list] = defaultdict(list)
    for r in all_chk_rows:
        k = (int(r.pond_id), int(r.lot_id))
        _chk_dates[k].append(r.checkpoint_date)
        _chk_rows[k].append(r)

    def _src_avg_w(src_pond_id: int, lot_id: int, mv_date) -> "float | None":
        k = (src_pond_id, lot_id)
        dates = _chk_dates.get(k)
        if not dates:
            return None
        idx = bisect.bisect_right(dates, mv_date) - 1
        if idx < 0:
            return None
        e = _chk_rows[k][idx]
        return float(e.avg_weight_g) if e.avg_weight_g else None

    # Filtro de fecha inferior: movimientos anteriores al checkpoint más antiguo
    # ya están absorbidos en biomass_kg; no es necesario cargarlos.
    _earliest_chk = None
    for _pk in active:
        _c = checkpoints.get(_pk)
        if _c is not None and (_earliest_chk is None or _c.checkpoint_date < _earliest_chk):
            _earliest_chk = _c.checkpoint_date

    _mv_params: dict = {"lot_ids": active_lot_ids, "pond_ids": active_pond_ids, "target_dt": target_dt}
    _mv_extra = ""
    if _earliest_chk:
        _mv_params["earliest_chk"] = _earliest_chk
        _mv_extra = "AND movement_time::date > :earliest_chk"

    movements = db.execute(
        text(f"""
            SELECT source_pond_id, destiny_pond_id, lot_id,
                   fish_quantity, movement_time::date AS mv_date
            FROM ponds_movements
            WHERE lot_id = ANY(:lot_ids)
              AND movement_time <= :target_dt
              AND (source_pond_id = ANY(:pond_ids) OR destiny_pond_id = ANY(:pond_ids))
              {_mv_extra}
            ORDER BY movement_time
        """),
        _mv_params,
    ).fetchall()

    # Pre-agrupar movimientos por lote — evita el scan O(pares × todos_movimientos)
    mvs_by_lot: dict[int, list] = defaultdict(list)
    for mv in movements:
        mvs_by_lot[int(mv.lot_id)].append(mv)

    lot_names = {
        r.id: (r.name or r.internal_id or str(r.id))
        for r in db.query(Lot).filter(Lot.id.in_(active_lot_ids)).all()
    }

    alerts: list[str] = []
    alerted: set = set()
    result: dict[tuple[int, int], float] = {}

    for (pond_id, lot_id) in active:
        chk = checkpoints.get((pond_id, lot_id))
        lot_label = lot_names.get(lot_id, str(lot_id))

        if chk is None:
            key = ("no_chk", pond_id, lot_id)
            if key not in alerted:
                alerts.append(
                    f"Lote {lot_label} (estanque {pond_id}): sin muestreo — biomasa no disponible"
                )
                alerted.add(key)
            result[(pond_id, lot_id)] = 0.0
            continue

        chk_date  = chk.checkpoint_date
        chk_avg_w = float(chk.avg_weight_g) if chk.avg_weight_g else 0.0
        proj = float(chk.biomass_kg)

        for mv in mvs_by_lot.get(lot_id, []):
            mv_date = mv.mv_date
            if mv_date <= chk_date:
                continue
            qty = int(mv.fish_quantity or 0)
            if qty <= 0:
                continue

            src = mv.source_pond_id
            dst = mv.destiny_pond_id

            if src == pond_id:
                proj -= qty * chk_avg_w / 1000.0
            elif dst == pond_id and src is not None:
                src_w = _src_avg_w(int(src), lot_id, mv_date)
                if src_w is not None:
                    proj += qty * src_w / 1000.0
                else:
                    key = ("no_src", int(src), lot_id)
                    if key not in alerted:
                        alerts.append(
                            f"Lote {lot_label}: llegada desde estanque {src} sin muestreo previo"
                        )
                        alerted.add(key)
                    proj += qty * chk_avg_w / 1000.0

        result[(pond_id, lot_id)] = max(0.0, proj)

    return result, alerts


def _allocate_biomass_by_pond_lot(db: Session) -> tuple[dict[int, float], dict[tuple[int, int], float]]:
    """OBSOLETA — reemplazada por _project_biomass_from_checkpoint. No llamar."""
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

    # 4) avg_weight por (pond_id, lot_id) + fallback por lote (mejor estimado de cualquier estanque)
    # Solo se usan entradas con sampled_at confirmado (sesión cerrada).
    # Las filas con sampled_at IS NULL provienen de sesiones nunca cerradas y pueden tener pesos erróneos.
    pond_lot_stats_rows = (
        db.query(PondLotStats.pond_id, PondLotStats.lot_id, PondLotStats.avg_weight)
        .filter(
            PondLotStats.lot_id.isnot(None),
            PondLotStats.pond_id.isnot(None),
            PondLotStats.sampled_at.isnot(None),
        )
        .all()
    )
    avg_weight_by_pond_lot: dict[tuple[int, int], float] = {
        (int(r.pond_id), int(r.lot_id)): float(r.avg_weight)
        for r in pond_lot_stats_rows
        if r.avg_weight
    }
    # Fallback de peso a nivel de lote: promedio rolling 90 días desde sampling_records
    # (más representativo que el primer pond_lot_stats encontrado, que puede ser n=1 o muy antiguo).
    # Si no hay datos recientes para un lote, se usa cualquier pond_lot_stats disponible.
    all_lot_ids_in_ponds = {lid for (_, lid) in pond_lot_count_map}
    lot_fallback_weight: dict[int, float] = _build_lot_rolling_avg(
        all_lot_ids_in_ponds, datetime.utcnow(), db
    )
    for (_, lid), w in avg_weight_by_pond_lot.items():
        if lid not in lot_fallback_weight:
            lot_fallback_weight[lid] = w

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
            # Estanque "dark": sin biomass_current medida → no hay base confiable para estimar.
            # El fallback de checkpoint en los reportes cubre este caso.
            continue

        weighted: dict[int, float] = {}
        for lot_id, n_fish in lot_rows:
            lot_avg_w = avg_weight_by_pond_lot.get((pond_id, lot_id))
            if not lot_avg_w:
                lot_avg_w = lot_fallback_weight.get(lot_id) or pond_avg_weight.get(pond_id, 1.0)
            weighted[lot_id] = float(n_fish) * float(lot_avg_w)

        denom = sum(weighted.values())
        if denom <= 0:
            continue

        # Si biomass_current < 50 % de la biomasa esperada (qty × peso estimado),
        # el valor acumulado es obsoleto: los peces crecieron o ingresaron sin
        # actualizar el registro.  Se estima directamente en lugar de distribuir
        # el valor incorrecto.
        expected_kg = denom / 1000.0
        if biomass < expected_kg * 0.50:
            for lot_id, w_g_n in weighted.items():
                estimated = w_g_n / 1000.0
                pond_lot_biomass_map[(pond_id, lot_id)] = estimated
                lot_biomass_map[lot_id] += estimated
            continue

        for lot_id, w in weighted.items():
            alloc = biomass * (w / denom)
            pond_lot_biomass_map[(pond_id, lot_id)] = alloc
            lot_biomass_map[lot_id] += alloc

    # Segundo paso: estanques "dark" (sin biomass_current).
    # Para pares (pond, lot) que no recibieron asignación, se estima con
    # qty × mejor_peso_disponible.  Menos preciso que la asignación proporcional
    # pero evita que lotes en estanques sin muestreo queden en 0.
    for (pond_id, lot_id), n_fish in pond_lot_count_map.items():
        if (pond_id, lot_id) in pond_lot_biomass_map:
            continue  # ya fue asignado en el paso principal
        w_g = (
            avg_weight_by_pond_lot.get((pond_id, lot_id))
            or lot_fallback_weight.get(lot_id)
        )
        if not w_g or n_fish <= 0:
            continue
        estimated = float(n_fish) * float(w_g) / 1000.0
        pond_lot_biomass_map[(pond_id, lot_id)] = estimated
        lot_biomass_map[lot_id] += estimated

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
    from datetime import date as _date
    pond_lot_biomass_map, _ = _project_biomass_from_checkpoint(_date.today(), db)

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
            "species_id": lot.species_id,
            "species": species_map.get(lot.species_id, "—"),
            "hatch_year": lot.hatch_year,
            "origin": lot.origin,
            "national": lot.national,
        },
        "species_list": [{"id": s.id, "name": s.name} for s in db.query(Species).order_by(Species.name).all()],
        "pond_rows": pond_rows,
        "stat_fish": total_fish,
        "stat_biomass": round(total_biomass, 1) if total_biomass else None,
        "stat_k": avg_k,
        "stat_avg_weight": avg_w_global,
        "msg": request.query_params.get("msg"),
        "status_msg": request.query_params.get("status", "ok"),
    })
    return HTMLResponse(content=html)


@router.post("/ui/lots/{lot_id}/edit", response_class=HTMLResponse)
def ui_lot_edit(
    lot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    internal_id: str = Form(""),
    species_id: int = Form(...),
    hatch_year: str = Form(""),
    origin: str = Form(""),
    national: str = Form(""),
):
    lot = db.query(Lot).filter(Lot.id == lot_id).first()
    if not lot:
        raise HTTPException(status_code=404, detail="Lot not found")
    lot.name = name.strip()
    lot.internal_id = internal_id.strip() or None
    lot.species_id = species_id
    lot.hatch_year = int(hatch_year) if hatch_year.strip().isdigit() else None
    lot.origin = origin.strip() or None
    lot.national = (national == "1")
    db.commit()
    return RedirectResponse(
        url=f"/views/ui/lots/{lot_id}?status=ok&msg=Lote+actualizado",
        status_code=303,
    )


# ── Vista Faena ─────────────────────────────────────────────────────────────

@router.post("/ui/sanitary-report", response_class=HTMLResponse)
def ui_add_sanitary_report(
    request: Request,
    db: Session = Depends(get_db),
    report_number: str = Form(...),
    laboratory: str = Form(...),
    report_date: str = Form(...),
):
    from datetime import date as _date_type
    from fastapi.responses import RedirectResponse
    try:
        parsed_date = _date_type.fromisoformat(report_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Fecha inválida")
    now = datetime.utcnow()
    report = SanitaryReport(
        report_number=report_number.strip(),
        laboratory=laboratory.strip(),
        report_date=parsed_date,
        created_at=now,
        updated_at=now,
    )
    db.add(report)
    db.commit()
    return RedirectResponse(url="/views/ui/faena", status_code=303)


@router.get("/ui/feed-legacy", response_class=HTMLResponse)
def ui_feed_legacy(request: Request, db: Session = Depends(get_db)):
    """Vista legacy de alimentacion (reemplazada por feed_endpoints)."""
    total_ponds = db.query(func.count(Pond.id)).scalar() or 0
    total_lots = db.query(func.count(Lot.id)).scalar() or 0
    total_fish = (
        db.query(func.count(Fish.id))
        .filter(Fish.state.in_(["alive", "depuration", "faena"]))
        .scalar()
        or 0
    )
    from datetime import date as _date
    _proj_feed, _ = _project_biomass_from_checkpoint(_date.today(), db)
    total_biomass = sum(_proj_feed.values())

    template = jinja_env.get_template("feed.html")
    html = template.render({
        "request": request,
        "stat_ponds": int(total_ponds),
        "stat_lots": int(total_lots),
        "stat_fish": int(total_fish),
        "stat_biomass": float(total_biomass),
    })
    return HTMLResponse(content=html)


@router.get("/ui/feed/inventory-legacy", response_class=HTMLResponse)
def ui_feed_inventory_legacy(request: Request, db: Session = Depends(get_db)):
    """Vista legacy de inventario feed (reemplazada por feed_endpoints)."""
    total_ponds = db.query(func.count(Pond.id)).scalar() or 0
    total_lots = db.query(func.count(Lot.id)).scalar() or 0
    total_fish = (
        db.query(func.count(Fish.id))
        .filter(Fish.state.in_(["alive", "depuration", "faena"]))
        .scalar()
        or 0
    )
    from datetime import date as _date
    _proj_inv, _ = _project_biomass_from_checkpoint(_date.today(), db)
    total_biomass = sum(_proj_inv.values())

    template = jinja_env.get_template("feed_inventory.html")
    html = template.render({
        "request": request,
        "stat_ponds": int(total_ponds),
        "stat_lots": int(total_lots),
        "stat_fish": int(total_fish),
        "stat_biomass": float(total_biomass),
    })
    return HTMLResponse(content=html)


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

    # Último informe sanitario por lote
    # Último informe sanitario del centro (aplica globalmente)
    latest_sanitary = (
        db.query(SanitaryReport)
        .order_by(SanitaryReport.report_date.desc())
        .first()
    )
    from datetime import date as _date, timedelta as _timedelta
    sanitary_expiry = None
    sanitary_expiry_warning = False
    if latest_sanitary and latest_sanitary.report_date:
        sanitary_expiry = latest_sanitary.report_date + _timedelta(days=365)
        sanitary_expiry_warning = sanitary_expiry <= (_date.today() + _timedelta(days=60))

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
            "lot": (lot.name or lot.internal_id) if lot else "N/D",
            "lot_id": fish.lot_id,
            "sex": sex_label,
            "sex_value": "f" if is_female else ("m" if sex_norm in ["m", "male", "macho"] else ""),
            "weight_g": weight_g,
            "weight_kg": weight_kg,
            "caviar_diameter": caviar_diameter,
            "dev_state": dev_state,
            "depuration_days": dep_days,
            "source_pond": source_pond.name if source_pond else "N/D",
            "source_pond_id": source_pond.id if source_pond else None,
            "dispatched_at": dispatched_at,
            "dispatched_at_display": _format_local_datetime(dispatched_at),
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

    source_pond_options = sorted(
        [
            {"id": p.id, "name": p.name}
            for p in ponds_map.values()
        ],
        key=lambda x: x["name"],
    )

    jaula_unit_ids = [
        int(u.id)
        for u in db.query(CultivationUnit.id, CultivationUnit.name)
        .filter(func.lower(CultivationUnit.name).like("%jaula%"))
        .all()
    ]
    jaula_pond_scope_options = []
    if jaula_unit_ids:
        jaula_pond_scope_options = [
            {"id": int(p.id), "name": p.name}
            for p in db.query(Pond.id, Pond.name)
            .filter(Pond.cultivation_unit_id.in_(jaula_unit_ids))
            .order_by(Pond.name.asc(), Pond.id.asc())
            .all()
        ]

    decl_scope_selected = (request.query_params.get("decl_scope") or "unit").strip().lower()
    valid_scope_values = {"unit"} | {f"pond:{item['id']}" for item in jaula_pond_scope_options}
    if decl_scope_selected not in valid_scope_values:
        decl_scope_selected = "unit"

    decl_status = request.query_params.get("decl_status")
    decl_folio = request.query_params.get("decl_folio")
    decl_id = request.query_params.get("decl_id")
    decl_msg = request.query_params.get("decl_msg")

    latest_declarations = []
    try:
        _decls = (
            db.query(CultivationDeclaration)
            .order_by(CultivationDeclaration.created_at.desc(), CultivationDeclaration.id.desc())
            .limit(10)
            .all()
        )
        latest_declarations = [
            {
                "id": d.id,
                "folio": d.folio,
                "source_pond_id": d.source_pond_id,
                "declaration_date": d.declaration_date,
                "status": d.status,
            }
            for d in _decls
        ]
    except Exception:
        latest_declarations = []

    # Faenas confirmadas (fish.state cambiado a dead tras confirmacion en planta)
    confirmed_date_raw = (request.query_params.get("confirmed_date") or "").strip()
    try:
        confirmed_date = datetime.strptime(confirmed_date_raw, "%Y-%m-%d").date() if confirmed_date_raw else datetime.now(APP_LOCAL_TZ).date()
    except ValueError:
        confirmed_date = datetime.now(APP_LOCAL_TZ).date()
    confirmed_date_value = confirmed_date.isoformat()

    confirmed_day_start_local = datetime(confirmed_date.year, confirmed_date.month, confirmed_date.day, tzinfo=APP_LOCAL_TZ)
    confirmed_day_end_local = confirmed_day_start_local + timedelta(days=1)
    confirmed_day_start_utc_naive = confirmed_day_start_local.astimezone(timezone.utc).replace(tzinfo=None)
    confirmed_day_end_utc_naive = confirmed_day_end_local.astimezone(timezone.utc).replace(tzinfo=None)

    confirmed_fish = (
        db.query(Fish)
        .filter(
            Fish.date_of_death.isnot(None),
            Fish.date_of_death >= confirmed_day_start_utc_naive,
            Fish.date_of_death < confirmed_day_end_utc_naive,
        )
        .order_by(Fish.date_of_death.desc(), Fish.id.desc())
        .all()
    )
    confirmed_fish_ids = [f.id for f in confirmed_fish]

    confirmed_mov_map: dict[int, PondMovement] = {}
    if confirmed_fish_ids:
        confirmed_movements = (
            db.query(PondMovement)
            .filter(
                PondMovement.fish_id.in_(confirmed_fish_ids),
                PondMovement.movement_reason == "faena",
                PondMovement.destiny_pond_id.is_(None),
            )
            .order_by(PondMovement.fish_id.asc(), PondMovement.movement_time.desc(), PondMovement.id.desc())
            .all()
        )
        for mv in confirmed_movements:
            if mv.fish_id not in confirmed_mov_map:
                confirmed_mov_map[mv.fish_id] = mv

    confirmed_lot_ids = {f.lot_id for f in confirmed_fish if f.lot_id}
    confirmed_lots_map: dict[int, Lot] = {}
    if confirmed_lot_ids:
        confirmed_lots_map = {
            lot.id: lot
            for lot in db.query(Lot).filter(Lot.id.in_(confirmed_lot_ids)).all()
        }

    confirmed_source_pond_ids = {
        mv.source_pond_id
        for mv in confirmed_mov_map.values()
        if mv.source_pond_id
    }
    confirmed_ponds_map: dict[int, Pond] = {}
    if confirmed_source_pond_ids:
        confirmed_ponds_map = {
            p.id: p
            for p in db.query(Pond).filter(Pond.id.in_(confirmed_source_pond_ids)).all()
        }

    confirmed_faena_rows = []
    for fish in confirmed_fish:
        mov = confirmed_mov_map.get(fish.id)
        # Solo consideramos confirmadas de faena (evita incluir mortalidades no asociadas)
        if not mov:
            continue
        lot = confirmed_lots_map.get(fish.lot_id)
        source_pond = confirmed_ponds_map.get(mov.source_pond_id) if mov.source_pond_id else None
        dispatched_at = mov.movement_time
        confirmed_at = fish.date_of_death
        dispatched_local = _to_local_datetime(dispatched_at)
        confirmed_local = _to_local_datetime(confirmed_at)
        same_dispatch_confirmation = bool(
            dispatched_local
            and confirmed_local
            and abs((confirmed_local - dispatched_local).total_seconds()) < 1
        )
        confirmed_faena_rows.append(
            {
                "fish_id": fish.id,
                "internal_id": fish.internal_id or "N/D",
                "lot": (lot.name or lot.internal_id) if lot else "N/D",
                "source_pond": source_pond.name if source_pond else "N/D",
                "dispatched_at": dispatched_at,
                "dispatched_at_display": _format_local_datetime(dispatched_at),
                "confirmed_at": confirmed_at,
                "confirmed_at_display": _format_local_datetime(confirmed_at),
                "same_dispatch_confirmation": same_dispatch_confirmation,
            }
        )

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
        "latest_sanitary": latest_sanitary,
        "sanitary_expiry": sanitary_expiry,
        "sanitary_expiry_warning": sanitary_expiry_warning,
        "source_pond_options": source_pond_options,
        "decl_status": decl_status,
        "decl_folio": decl_folio,
        "decl_id": decl_id,
        "decl_msg": decl_msg,
        "latest_declarations": latest_declarations,
        "decl_scope_selected": decl_scope_selected,
        "jaula_scope_options": jaula_pond_scope_options,
        "confirmed_date": confirmed_date_value,
        "confirmed_faena_rows": confirmed_faena_rows,
    })
    return HTMLResponse(content=html)
