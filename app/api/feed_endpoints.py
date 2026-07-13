from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from decimal import Decimal
import json
from app.db.session import SessionLocal
from app.models.feed import (
    FeedType,
    FeedReceiptHeader,
    FeedReceiptLine,
    FeedInventoryAdjustment,
    FeedStockLedger,
    FeedMovementType,
    FeedReceiptStatus,
    FeedProgram,
    FeedProgramLine,
    AccountingOutboxEvent,
)
from app.models.feed import FeedExecutionEvent
from app.models.ponds import Pond
from app.models.cultivation_units import CultivationUnit
from app.models.lots import Lot
from app.models.fish import Fish

router = APIRouter(prefix="/views", tags=["feed"])
template_dir = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)))

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════════
# UTILIDADES
# ════════════════════════════════════════════════════════════════════════════════

def calculate_feed_stock(feed_type_id: int, db: Session):
    """Stock fisico de un tipo de alimento.

    Modelo simple y auditable:
        disponible = (ingresos + ajustes) - ejecutado

    La planificacion (movimientos reserve/release) es informativa y NO mueve
    el stock fisico: confirmar una ejecucion descuenta del disponible
    exactamente los sacos consumidos (movimiento execute). Los movimientos
    historicos reserve/release quedan inertes para el calculo del disponible.

    'planned_bags' refleja el plan pendiente del programa vigente (no el ledger).
    """
    ledger_rows = db.query(FeedStockLedger).filter(
        FeedStockLedger.feed_type_id == feed_type_id
    ).all()

    total_bags = 0
    total_kg = Decimal("0")
    executed_bags = 0

    for entry in ledger_rows:
        delta_bags = entry.bags_delta or 0
        delta_kg = entry.kg_delta or Decimal("0")

        # total_bags = ingresos fisicos + ajustes (no movimientos internos)
        if entry.movement_type in (FeedMovementType.entry, FeedMovementType.adjustment):
            total_bags += delta_bags
            total_kg += delta_kg
        elif entry.movement_type == FeedMovementType.execute:
            # bags_delta es negativo en execute (-N consumidos)
            executed_bags += abs(delta_bags)
        # reserve / release: planificacion -> no afectan el stock fisico

    available_bags = total_bags - executed_bags

    # Plan pendiente del programa vigente (informativo, no afecta el disponible)
    planned_bags = int(
        db.query(func.coalesce(func.sum(FeedProgramLine.remaining_bags), 0))
        .join(FeedProgram, FeedProgram.id == FeedProgramLine.feed_program_id)
        .filter(
            FeedProgram.status == "open",
            FeedProgramLine.feed_type_id == feed_type_id,
        )
        .scalar()
        or 0
    )

    return {
        "total_bags": total_bags,
        "total_kg": total_kg,
        "available_bags": available_bags,
        "planned_bags": planned_bags,
        "executed_bags": executed_bags,
    }


def _sum_executed_for_program(program_id: int, db: Session) -> dict:
    """Retorna {(pond_id, feed_type_id): total_confirmed_bags} para un programa."""
    events = db.query(FeedExecutionEvent).filter(
        FeedExecutionEvent.feed_program_id == program_id
    ).all()
    result = {}
    for ev in events:
        key = (int(ev.pond_id), int(ev.feed_type_id))
        result[key] = result.get(key, 0) + int(ev.confirmed_bags)
    return result


# ════════════════════════════════════════════════════════════════════════════════
# FORMULARIOS Y VISTAS
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/ui/feed", response_class=HTMLResponse)
def ui_feed(
    request: Request,
    db: Session = Depends(get_db)
):
    """Vista principal — Confirmacion de ejecucion del programa vigente"""
    feed_types = db.query(FeedType).filter(FeedType.active == 1).order_by(FeedType.name).all()

    stock_by_feed = {}
    for ft in feed_types:
        stock_by_feed[ft.id] = calculate_feed_stock(ft.id, db)

    # Solo mostrar alimentos con sacos disponibles
    feed_types = [ft for ft in feed_types if stock_by_feed[ft.id]["available_bags"] > 0]

    open_program = (
        db.query(FeedProgram)
        .filter(FeedProgram.status == "open")
        .order_by(FeedProgram.created_at.desc(), FeedProgram.id.desc())
        .first()
    )

    # --- Mapas de planificado y ejecutado acumulado ---
    planned_map = {}   # (pond_id, ft_id) -> z_bags
    line_id_map = {}   # (pond_id, ft_id) -> program_line_id
    executed_map = {}  # (pond_id, ft_id) -> y_bags (acumulado en el programa)
    has_unplanned = False

    if open_program:
        lines = db.query(FeedProgramLine).filter(
            FeedProgramLine.feed_program_id == open_program.id
        ).all()
        for line in lines:
            key = (int(line.pond_id), int(line.feed_type_id))
            planned_map[key] = int(line.planned_bags or 0)
            line_id_map[key] = line.id

        executed_map = _sum_executed_for_program(open_program.id, db)

        has_unplanned = db.query(FeedExecutionEvent).filter(
            FeedExecutionEvent.feed_program_id == open_program.id,
            FeedExecutionEvent.is_unplanned == 1,
        ).first() is not None

    # --- Estanques padre con lotes activos (propios o de sus hijos) ---
    pond_rows_raw = (
        db.query(Pond, CultivationUnit)
        .outerjoin(CultivationUnit, CultivationUnit.id == Pond.cultivation_unit_id)
        .filter(Pond.parent_pond_id.is_(None))
        .order_by(
            func.coalesce(CultivationUnit.id, 999999),
            Pond.id,
        )
        .all()
    )

    # Pre-cargar todos los hijos de los padres
    parent_ids = [pond.id for pond, _ in pond_rows_raw]
    children_map = {}  # parent_id -> [children]
    if parent_ids:
        all_children = db.query(Pond).filter(Pond.parent_pond_id.in_(parent_ids)).all()
        for child in all_children:
            if child.parent_pond_id not in children_map:
                children_map[child.parent_pond_id] = []
            children_map[child.parent_pond_id].append(child)

    # Recopilar todos los lotes de padres + hijos
    lot_ids = set()
    for pond, _ in pond_rows_raw:
        if pond.active_lot_ids:
            for lid in pond.active_lot_ids:
                if lid:
                    lot_ids.add(int(lid))
        elif pond.lot_id:
            lot_ids.add(int(pond.lot_id))
        
        # Lotes de los hijos
        for child in children_map.get(pond.id, []):
            if child.active_lot_ids:
                for lid in child.active_lot_ids:
                    if lid:
                        lot_ids.add(int(lid))
            elif child.lot_id:
                lot_ids.add(int(child.lot_id))

    lot_map = {}
    if lot_ids:
        lots = db.query(Lot).filter(Lot.id.in_(list(lot_ids))).all()
        lot_map = {int(l.id): (l.internal_id or l.name) for l in lots}

    pond_rows = []
    for pond, cu in pond_rows_raw:
        # Obtener hijos del mapa pre-cargado
        children = children_map.get(pond.id, [])
        # Recopilar nombres de lotes del padre + hijos
        lot_names = []
        
        # Lotes del padre
        if pond.active_lot_ids:
            for lid in pond.active_lot_ids:
                label = lot_map.get(int(lid))
                if label:
                    lot_names.append(label)
        elif pond.lot_id and int(pond.lot_id) in lot_map:
            lot_names.append(lot_map[int(pond.lot_id)])
        
        # Lotes de los hijos
        for child in children:
            if child.active_lot_ids:
                for lid in child.active_lot_ids:
                    label = lot_map.get(int(lid))
                    if label and label not in lot_names:
                        lot_names.append(label)
            elif child.lot_id and int(child.lot_id) in lot_map:
                label = lot_map[int(child.lot_id)]
                if label not in lot_names:
                    lot_names.append(label)

        # Filtrar estanques sin lotes activos (pero SIEMPRE mostrar padres con hijos que tienen lotes)
        if not lot_names:
            continue

        feed_cells = []
        total_y = 0
        total_z = 0
        for ft in feed_types:
            # Suma: padre + todos los hijos
            z_bags = planned_map.get((int(pond.id), int(ft.id)), 0)
            y_bags = executed_map.get((int(pond.id), int(ft.id)), 0)
            
            for child in children:
                z_bags += planned_map.get((int(child.id), int(ft.id)), 0)
                y_bags += executed_map.get((int(child.id), int(ft.id)), 0)
            
            total_y += y_bags
            total_z += z_bags
            feed_cells.append({
                "feed_type_id": ft.id,
                "z_bags": z_bags,
                "y_bags": y_bags,
                "available_bags": stock_by_feed[ft.id]["available_bags"],
            })

        if total_y == 0:
            row_status = "empty"
        elif total_z > 0 and total_y >= total_z:
            row_status = "complete"
        else:
            row_status = "partial"

        pond_rows.append({
            "pond_id": pond.id,
            "pond_name": pond.name,
            "cultivation_unit_name": cu.name if cu else "Sin unidad",
            "biomass_estimated": float(pond.biomass_current or pond.biomass or 0),
            "lot_names": lot_names,
            "feed_cells": feed_cells,
            "total_y": total_y,
            "total_z": total_z,
            "row_status": row_status,
        })

    planned_cells_nonzero = sum(1 for v in planned_map.values() if int(v or 0) > 0)
    planned_bags_total = sum(int(v or 0) for v in planned_map.values())

    context = {
        "request": request,
        "feed_types": feed_types,
        "pond_rows": pond_rows,
        "stock_by_feed": stock_by_feed,
        "open_program": open_program,
        "has_open_program": open_program is not None,
        "has_unplanned": has_unplanned,
        "planned_cells_nonzero": planned_cells_nonzero,
        "planned_bags_total": planned_bags_total,
        "msg": request.query_params.get("msg"),
        "status_msg": request.query_params.get("status", "ok"),
    }
    template = jinja_env.get_template("feed_inventory_main.html")
    return HTMLResponse(template.render(context))


@router.get("/ui/feed/print", response_class=HTMLResponse)
def ui_feed_print(
    request: Request,
    db: Session = Depends(get_db)
):
    """Vista de impresión del programa vigente (apta para PDF)."""
    from datetime import datetime

    open_program = (
        db.query(FeedProgram)
        .filter(FeedProgram.status == "open")
        .order_by(FeedProgram.created_at.desc(), FeedProgram.id.desc())
        .first()
    )
    if not open_program:
        return HTMLResponse("<html><body><h2>No hay programa vigente para imprimir.</h2>"
                            "<a href='/views/ui/feed'>Volver</a></body></html>", status_code=200)

    feed_types = db.query(FeedType).filter(FeedType.active == 1).order_by(FeedType.name).all()

    stock_by_feed = {}
    for ft in feed_types:
        stock_by_feed[ft.id] = calculate_feed_stock(ft.id, db)

    # Solo mostrar alimentos con sacos disponibles
    feed_types = [ft for ft in feed_types if stock_by_feed[ft.id]["available_bags"] > 0]

    # Mapas de planificado y ejecutado
    lines = db.query(FeedProgramLine).filter(
        FeedProgramLine.feed_program_id == open_program.id
    ).all()
    planned_map = {}
    for line in lines:
        key = (int(line.pond_id), int(line.feed_type_id))
        planned_map[key] = int(line.planned_bags or 0)

    executed_map = _sum_executed_for_program(open_program.id, db)

    # Estanques padre
    pond_rows_raw = (
        db.query(Pond, CultivationUnit)
        .outerjoin(CultivationUnit, CultivationUnit.id == Pond.cultivation_unit_id)
        .filter(Pond.parent_pond_id.is_(None))
        .order_by(func.coalesce(CultivationUnit.id, 999999), Pond.id)
        .all()
    )

    parent_ids = [pond.id for pond, _ in pond_rows_raw]
    children_map = {}
    if parent_ids:
        all_children = db.query(Pond).filter(Pond.parent_pond_id.in_(parent_ids)).all()
        for child in all_children:
            children_map.setdefault(child.parent_pond_id, []).append(child)

    lot_ids = set()
    for pond, _ in pond_rows_raw:
        for src in [pond] + children_map.get(pond.id, []):
            if src.active_lot_ids:
                lot_ids.update(int(lid) for lid in src.active_lot_ids if lid)
            elif src.lot_id:
                lot_ids.add(int(src.lot_id))

    lot_map = {}
    if lot_ids:
        lots = db.query(Lot).filter(Lot.id.in_(list(lot_ids))).all()
        lot_map = {int(l.id): (l.internal_id or l.name) for l in lots}

    rows = []
    for pond, cu in pond_rows_raw:
        children = children_map.get(pond.id, [])
        lot_names = []
        for src in [pond] + children:
            if src.active_lot_ids:
                for lid in src.active_lot_ids:
                    label = lot_map.get(int(lid))
                    if label and label not in lot_names:
                        lot_names.append(label)
            elif src.lot_id:
                label = lot_map.get(int(src.lot_id))
                if label and label not in lot_names:
                    lot_names.append(label)

        if not lot_names:
            continue

        bags_by_ft = {}
        total_z = 0
        for ft in feed_types:
            z = planned_map.get((int(pond.id), int(ft.id)), 0)
            for child in children:
                z += planned_map.get((int(child.id), int(ft.id)), 0)
            bags_by_ft[ft.id] = z
            total_z += z

        rows.append({
            "pond_name": pond.name,
            "cultivation_unit_name": cu.name if cu else "Sin unidad",
            "lot_names": lot_names,
            "bags_by_ft": bags_by_ft,
            "total_z": total_z,
        })

    # Solo imprimir estanques con programa > 0
    rows = [r for r in rows if r["total_z"] > 0]

    total_programmed = sum(r["total_z"] for r in rows)
    rows_with_program = len(rows)

    context = {
        "request": request,
        "program": open_program,
        "feed_types": feed_types,
        "stock_by_feed": stock_by_feed,
        "rows": rows,
        "total_programmed": total_programmed,
        "rows_with_program": rows_with_program,
        "print_date": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }
    template = jinja_env.get_template("feed_print.html")
    return HTMLResponse(template.render(context))


@router.get("/ui/feed/inventory")
def ui_feed_inventory_legacy_redirect():
    return RedirectResponse(url="/views/ui/inventory", status_code=303)


@router.get("/ui/inventory", response_class=HTMLResponse)
def ui_feed_inventory(
    request: Request,
    db: Session = Depends(get_db)
):
    """Vista contable del inventario disponible por tipo de alimento."""
    feed_types = db.query(FeedType).filter(FeedType.active == 1).order_by(FeedType.name).all()

    adjustment_rows = (
        db.query(FeedInventoryAdjustment)
        .order_by(FeedInventoryAdjustment.created_at.desc(), FeedInventoryAdjustment.id.desc())
        .all()
    )
    latest_adjustment_by_feed = {}
    for row in adjustment_rows:
        feed_id = int(row.feed_type_id)
        if feed_id not in latest_adjustment_by_feed:
            latest_adjustment_by_feed[feed_id] = row

    # Programa vigente (ultimo cargado): "ejecutado" y "programado" se miden
    # contra este programa, no contra el historico.
    open_program = (
        db.query(FeedProgram)
        .filter(FeedProgram.status == "open")
        .order_by(FeedProgram.created_at.desc(), FeedProgram.id.desc())
        .first()
    )
    planned_by_ft = {}   # feed_type_id -> sacos planificados en el programa vigente
    executed_by_ft = {}  # feed_type_id -> sacos confirmados en el programa vigente
    if open_program:
        planned_by_ft = dict(
            db.query(
                FeedProgramLine.feed_type_id,
                func.coalesce(func.sum(FeedProgramLine.planned_bags), 0),
            )
            .filter(FeedProgramLine.feed_program_id == open_program.id)
            .group_by(FeedProgramLine.feed_type_id)
            .all()
        )
        executed_by_ft = dict(
            db.query(
                FeedExecutionEvent.feed_type_id,
                func.coalesce(func.sum(FeedExecutionEvent.confirmed_bags), 0),
            )
            .filter(FeedExecutionEvent.feed_program_id == open_program.id)
            .group_by(FeedExecutionEvent.feed_type_id)
            .all()
        )

    inventory_rows = []
    total_bags = 0
    total_available = 0
    total_planned = 0
    total_executed = 0

    for feed_type in feed_types:
        stock = calculate_feed_stock(feed_type.id, db)
        # Sacos Totales = stock fisico de bodega = ingresos + ajustes - ejecutado(historico)
        sacos_totales = int(stock["available_bags"])
        # Ejecutado / Programado referidos al programa vigente
        ejecutado = int(executed_by_ft.get(feed_type.id, 0))
        plan_vigente = int(planned_by_ft.get(feed_type.id, 0))
        programado = max(plan_vigente - ejecutado, 0)
        # Disponibles para programar = stock fisico - lo aun comprometido por el plan
        disponibles = sacos_totales - programado

        latest_adjustment = latest_adjustment_by_feed.get(int(feed_type.id))
        inventory_rows.append({
            "feed_type_id": feed_type.id,
            "feed_name": feed_type.name,
            "total_bags": sacos_totales,
            "available_bags": disponibles,
            "planned_bags": programado,
            "executed_bags": ejecutado,
            "latest_comment": latest_adjustment.comment if latest_adjustment else "",
            "latest_adjustment_at": latest_adjustment.created_at if latest_adjustment else None,
        })
        total_bags += sacos_totales
        total_available += disponibles
        total_planned += programado
        total_executed += ejecutado

    context = {
        "request": request,
        "inventory_rows": inventory_rows,
        "total_bags": total_bags,
        "total_available": total_available,
        "total_planned": total_planned,
        "total_executed": total_executed,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    }
    template = jinja_env.get_template("feed_inventory.html")
    return HTMLResponse(template.render(context))


# ════════════════════════════════════════════════════════════════════════════════
# CONFIRMACION DE EJECUCION
# ════════════════════════════════════════════════════════════════════════════════

@router.post("/ui/feed/confirm-row")
async def ui_feed_confirm_row(
    request: Request,
    db: Session = Depends(get_db),
):
    """Confirma los sacos de una fila (estanque) contra el programa vigente. Sin recarga de pagina."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "JSON invalido"}, status_code=400)

    program_id = payload.get("program_id")
    pond_id = int(payload.get("pond_id", 0))
    cells = payload.get("cells", [])

    program = db.query(FeedProgram).filter(
        FeedProgram.id == program_id, FeedProgram.status == "open"
    ).first()
    if not program:
        return JSONResponse({"success": False, "error": "No hay programa vigente"}, status_code=400)

    now = datetime.utcnow()
    updated_cells = []

    try:
        for cell in cells:
            feed_type_id = int(cell.get("feed_type_id"))
            bags = int(cell.get("bags") or 0)
            if bags <= 0:
                # Devolver estado actual sin cambios
                line = db.query(FeedProgramLine).filter(
                    FeedProgramLine.feed_program_id == program_id,
                    FeedProgramLine.pond_id == pond_id,
                    FeedProgramLine.feed_type_id == feed_type_id,
                ).first()
                stock = calculate_feed_stock(feed_type_id, db)
                exec_events = db.query(FeedExecutionEvent).filter(
                    FeedExecutionEvent.feed_program_id == program_id,
                    FeedExecutionEvent.pond_id == pond_id,
                    FeedExecutionEvent.feed_type_id == feed_type_id,
                ).all()
                updated_cells.append({
                    "feed_type_id": feed_type_id,
                    "y_bags": sum(e.confirmed_bags for e in exec_events),
                    "z_bags": int(line.planned_bags if line else 0),
                    "available_bags": stock["available_bags"],
                })
                continue

            line = db.query(FeedProgramLine).filter(
                FeedProgramLine.feed_program_id == program_id,
                FeedProgramLine.pond_id == pond_id,
                FeedProgramLine.feed_type_id == feed_type_id,
            ).first()

            planned_bags = int(line.planned_bags if line else 0)
            is_unplanned = 1 if planned_bags == 0 else 0

            # Validar stock disponible para no planificados
            if is_unplanned:
                stock_before = calculate_feed_stock(feed_type_id, db)
                if stock_before["available_bags"] < bags:
                    return JSONResponse({
                        "success": False,
                        "error": f"Stock insuficiente para '{feed_type_id}' (disponible: {stock_before['available_bags']} sacos)",
                    }, status_code=400)

            # Crear evento de ejecucion
            event = FeedExecutionEvent(
                feed_program_id=program_id,
                feed_program_line_id=line.id if line else None,
                pond_id=pond_id,
                feed_type_id=feed_type_id,
                confirmed_bags=bags,
                confirmed_kg=Decimal("0"),
                is_unplanned=is_unplanned,
                confirmed_at=now,
                confirmed_by="system",
            )
            db.add(event)
            db.flush()

            # Execute: descuenta del disponible los sacos realmente consumidos
            # (delta negativo). La planificacion no mueve stock fisico.
            db.add(FeedStockLedger(
                feed_type_id=feed_type_id,
                movement_type=FeedMovementType.execute,
                bags_delta=-bags,
                kg_delta=Decimal("0"),
                source_table="feed_execution_events",
                source_id=event.id,
                created_at=now,
                created_by="system",
            ))

            # Actualizar FeedProgramLine
            if line:
                line.executed_bags = int(line.executed_bags or 0) + bags
                line.remaining_bags = max(0, int(line.planned_bags or 0) - int(line.executed_bags))
                line.updated_at = now

            db.flush()

            # Stock actualizado
            stock_after = calculate_feed_stock(feed_type_id, db)
            exec_events = db.query(FeedExecutionEvent).filter(
                FeedExecutionEvent.feed_program_id == program_id,
                FeedExecutionEvent.pond_id == pond_id,
                FeedExecutionEvent.feed_type_id == feed_type_id,
            ).all()
            total_y = sum(e.confirmed_bags for e in exec_events)

            updated_cells.append({
                "feed_type_id": feed_type_id,
                "y_bags": total_y,
                "z_bags": planned_bags,
                "available_bags": stock_after["available_bags"],
            })

        db.commit()

        total_y = sum(c["y_bags"] for c in updated_cells)
        total_z = sum(c["z_bags"] for c in updated_cells)
        if total_y == 0:
            row_status = "empty"
        elif total_z > 0 and total_y >= total_z:
            row_status = "complete"
        else:
            row_status = "partial"

        has_unplanned = db.query(FeedExecutionEvent).filter(
            FeedExecutionEvent.feed_program_id == program_id,
            FeedExecutionEvent.is_unplanned == 1,
        ).first() is not None

        return JSONResponse({
            "success": True,
            "updated_cells": updated_cells,
            "row_status": row_status,
            "has_unplanned": has_unplanned,
        })
    except Exception as e:
        db.rollback()
        return JSONResponse({"success": False, "error": f"Error: {str(e)}"}, status_code=500)


@router.post("/ui/feed/confirm-all")
async def ui_feed_confirm_all(
    request: Request,
    db: Session = Depends(get_db),
):
    """Confirma todos los sacos ingresados en el formulario completo."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "JSON invalido"}, status_code=400)

    program_id = payload.get("program_id")
    rows = payload.get("rows", [])

    program = db.query(FeedProgram).filter(
        FeedProgram.id == program_id, FeedProgram.status == "open"
    ).first()
    if not program:
        return JSONResponse({"success": False, "error": "No hay programa vigente"}, status_code=400)

    feed_types_active = {
        int(ft.id): ft for ft in db.query(FeedType).filter(FeedType.active == 1).all()
    }
    now = datetime.utcnow()
    results = []

    try:
        for row in rows:
            pond_id = int(row.get("pond_id", 0))
            cells = row.get("cells", [])
            updated_cells = []

            for cell in cells:
                feed_type_id = int(cell.get("feed_type_id"))
                bags = int(cell.get("bags") or 0)
                if bags <= 0:
                    continue

                if feed_type_id not in feed_types_active:
                    continue

                line = db.query(FeedProgramLine).filter(
                    FeedProgramLine.feed_program_id == program_id,
                    FeedProgramLine.pond_id == pond_id,
                    FeedProgramLine.feed_type_id == feed_type_id,
                ).first()

                planned_bags = int(line.planned_bags if line else 0)
                is_unplanned = 1 if planned_bags == 0 else 0

                if is_unplanned:
                    stock_before = calculate_feed_stock(feed_type_id, db)
                    if stock_before["available_bags"] < bags:
                        return JSONResponse({
                            "success": False,
                            "error": f"Stock insuficiente para estanque {pond_id} / tipo {feed_type_id}",
                        }, status_code=400)

                event = FeedExecutionEvent(
                    feed_program_id=program_id,
                    feed_program_line_id=line.id if line else None,
                    pond_id=pond_id,
                    feed_type_id=feed_type_id,
                    confirmed_bags=bags,
                    confirmed_kg=Decimal("0"),
                    is_unplanned=is_unplanned,
                    confirmed_at=now,
                    confirmed_by="system",
                )
                db.add(event)
                db.flush()

                # Execute: descuenta del disponible los sacos consumidos.
                db.add(FeedStockLedger(
                    feed_type_id=feed_type_id,
                    movement_type=FeedMovementType.execute,
                    bags_delta=-bags,
                    kg_delta=Decimal("0"),
                    source_table="feed_execution_events",
                    source_id=event.id,
                    created_at=now,
                    created_by="system",
                ))

                if line:
                    line.executed_bags = int(line.executed_bags or 0) + bags
                    line.remaining_bags = max(0, int(line.planned_bags or 0) - int(line.executed_bags))
                    line.updated_at = now

                updated_cells.append({"feed_type_id": feed_type_id, "bags": bags})

            results.append({"pond_id": pond_id, "confirmed_cells": len(updated_cells)})

        db.commit()
        return JSONResponse({"success": True, "results": results})
    except Exception as e:
        db.rollback()
        return JSONResponse({"success": False, "error": f"Error: {str(e)}"}, status_code=500)


@router.get("/ui/feed/inventory/load-new")
def ui_feed_load_new_legacy_redirect():
    return RedirectResponse(url="/views/ui/inventory/load-new", status_code=303)


@router.get("/ui/inventory/load-new", response_class=HTMLResponse)
def ui_feed_load_new(
    request: Request,
    db: Session = Depends(get_db)
):
    """Vista del formulario para ingresar alimento"""
    feed_types = db.query(FeedType).filter(FeedType.active == 1).order_by(FeedType.name).all()
    
    feed_types_json = json.dumps([
        {"id": ft.id, "name": ft.name}
        for ft in feed_types
    ])
    
    context = {
        "request": request,
        "feed_types_json": feed_types_json,
    }
    template = jinja_env.get_template("feed_load_new.html")
    return HTMLResponse(template.render(context))


@router.get("/ui/feed/inventory/adjustment-new")
def ui_feed_adjustment_new_legacy_redirect():
    return RedirectResponse(url="/views/ui/inventory/adjustment-new", status_code=303)


@router.get("/ui/inventory/adjustment-new", response_class=HTMLResponse)
def ui_feed_adjustment_new(
    request: Request,
    db: Session = Depends(get_db)
):
    """Vista del formulario para cuadre de inventario"""
    feed_types = db.query(FeedType).filter(FeedType.active == 1).order_by(FeedType.name).all()
    
    context = {
        "request": request,
        "feed_types": feed_types,
        "stock_data_json": "{}",
    }
    template = jinja_env.get_template("feed_adjustment_new.html")
    return HTMLResponse(template.render(context))


# ════════════════════════════════════════════════════════════════════════════════
# ENDPOINTS DE GUARDADO (POST)
# ════════════════════════════════════════════════════════════════════════════════

@router.post("/ui/feed/inventory/load-save")
@router.post("/ui/inventory/load-save")
async def ui_feed_load_save(
    request: Request,
    db: Session = Depends(get_db)
):
    """Guardar carga de alimento (guía + líneas)"""
    try:
        payload = await request.json()
    except Exception as e:
        return JSONResponse({"success": False, "error": f"Error al procesar JSON: {str(e)}"}, status_code=400)

    # Validaciones de cabecera
    guia_despacho = (payload.get("guia_despacho") or "").strip()
    if not guia_despacho:
        return JSONResponse({"success": False, "error": "Guía de despacho es requerida"}, status_code=422)

    fecha_ingreso_str = (payload.get("fecha_ingreso") or "").strip()
    if not fecha_ingreso_str:
        return JSONResponse({"success": False, "error": "Fecha de ingreso es requerida"}, status_code=422)

    try:
        fecha_ingreso = datetime.strptime(fecha_ingreso_str, "%Y-%m-%d")
    except ValueError:
        return JSONResponse({"success": False, "error": "Fecha de ingreso con formato inválido"}, status_code=422)

    # Validar unicidad de guía
    existing = db.query(FeedReceiptHeader).filter(
        FeedReceiptHeader.guia_despacho == guia_despacho
    ).first()
    if existing:
        return JSONResponse({"success": False, "error": f"La guía {guia_despacho} ya existe en el sistema"}, status_code=409)

    # Validar líneas
    lines = payload.get("lines", [])
    if not lines:
        return JSONResponse({"success": False, "error": "Debes agregar al menos una línea de producto"}, status_code=422)

    validated_lines = []
    for i, line in enumerate(lines):
        feed_type_id = line.get("feed_type_id")
        quantity_kg = line.get("quantity_kg")
        bag_weight_kg = line.get("bag_weight_kg")
        quantity_bags = line.get("quantity_bags")

        if not feed_type_id or not quantity_kg or not bag_weight_kg:
            return JSONResponse({"success": False, "error": f"Línea {i+1}: faltan datos obligatorios"}, status_code=422)

        feed_type_id = int(feed_type_id)
        quantity_kg = float(quantity_kg)
        bag_weight_kg = int(bag_weight_kg)
        quantity_bags = int(quantity_bags or 0)

        # Validaciones
        if bag_weight_kg not in (20, 25):
            return JSONResponse({"success": False, "error": f"Línea {i+1}: peso de saco debe ser 20 o 25 kg"}, status_code=422)

        if quantity_kg <= 0:
            return JSONResponse({"success": False, "error": f"Línea {i+1}: cantidad debe ser positiva"}, status_code=422)

        # Validar que cantidad_kg sea múltiplo de peso_saco
        if quantity_kg % bag_weight_kg != 0:
            return JSONResponse({"success": False, "error": f"Línea {i+1}: {quantity_kg} kg no es múltiplo de {bag_weight_kg} kg/saco"}, status_code=422)

        # Validar que el tipo de alimento existe
        feed_type = db.query(FeedType).filter(FeedType.id == feed_type_id, FeedType.active == 1).first()
        if not feed_type:
            return JSONResponse({"success": False, "error": f"Línea {i+1}: tipo de alimento no válido"}, status_code=422)

        validated_lines.append({
            "feed_type_id": feed_type_id,
            "quantity_kg": Decimal(str(quantity_kg)),
            "bag_weight_kg": bag_weight_kg,
            "quantity_bags": int(quantity_kg // bag_weight_kg),
        })

    # Crear cabecera y líneas
    now = datetime.utcnow()
    try:
        header = FeedReceiptHeader(
            guia_despacho=guia_despacho,
            supplier_name=(payload.get("supplier_name") or "").strip() or None,
            fecha_ingreso=fecha_ingreso,
            status=FeedReceiptStatus.confirmed,
            created_by="system",  # TODO: obtener del usuario autenticado
            created_at=now,
            confirmed_at=now,
            updated_at=now,
        )
        db.add(header)
        db.flush()  # Para obtener el ID

        # Crear líneas y entries en ledger
        for line_data in validated_lines:
            line = FeedReceiptLine(
                feed_receipt_id=header.id,
                feed_type_id=line_data["feed_type_id"],
                quantity_kg=line_data["quantity_kg"],
                bag_weight_kg=line_data["bag_weight_kg"],
                quantity_bags=line_data["quantity_bags"],
                created_at=now,
                updated_at=now,
            )
            db.add(line)
            db.flush()

            # Crear entrada en ledger
            ledger_entry = FeedStockLedger(
                feed_type_id=line_data["feed_type_id"],
                movement_type=FeedMovementType.entry,
                bags_delta=line_data["quantity_bags"],
                kg_delta=line_data["quantity_kg"],
                source_table="feed_receipt_headers",
                source_id=header.id,
                created_at=now,
                created_by="system",
            )
            db.add(ledger_entry)

            # Crear evento para outbox (integración futura con Contabilidad)
            outbox_event = AccountingOutboxEvent(
                event_type="feed_entry_created",
                payload_json=json.dumps({
                    "guia_despacho": guia_despacho,
                    "feed_type_id": line_data["feed_type_id"],
                    "quantity_kg": str(line_data["quantity_kg"]),
                    "quantity_bags": line_data["quantity_bags"],
                    "fecha_ingreso": fecha_ingreso.isoformat(),
                }),
                status="pending",
                created_at=now,
            )
            db.add(outbox_event)

        db.commit()
        return JSONResponse({"success": True, "receipt_id": header.id})
    except Exception as e:
        db.rollback()
        return JSONResponse({"success": False, "error": f"Error al guardar: {str(e)}"}, status_code=500)


@router.post("/ui/feed/inventory/adjustment-save")
@router.post("/ui/inventory/adjustment-save")
async def ui_feed_adjustment_save(
    request: Request,
    db: Session = Depends(get_db)
):
    """Guardar cuadre de inventario"""
    try:
        payload = await request.json()
    except Exception as e:
        return JSONResponse({"success": False, "error": f"Error al procesar JSON: {str(e)}"}, status_code=400)

    feed_type_id = payload.get("feed_type_id")
    stock_calculated_bags = payload.get("stock_calculated_bags")
    stock_real_bags = payload.get("stock_real_bags")
    difference_bags = payload.get("difference_bags")
    comment = (payload.get("comment") or "").strip()

    # Validaciones
    if not feed_type_id:
        return JSONResponse({"success": False, "error": "Tipo de alimento es requerido"}, status_code=422)

    if stock_real_bags is None:
        return JSONResponse({"success": False, "error": "Stock real es requerido"}, status_code=422)

    if not comment:
        return JSONResponse({"success": False, "error": "Comentario es requerido"}, status_code=422)

    # Validar que el tipo de alimento existe
    feed_type = db.query(FeedType).filter(FeedType.id == feed_type_id, FeedType.active == 1).first()
    if not feed_type:
        return JSONResponse({"success": False, "error": "Tipo de alimento no válido"}, status_code=422)

    # Guardar ajuste
    now = datetime.utcnow()
    try:
        adjustment = FeedInventoryAdjustment(
            feed_type_id=feed_type_id,
            stock_calculated_bags=int(stock_calculated_bags or 0),
            stock_real_bags=int(stock_real_bags),
            difference_bags=int(difference_bags or 0),
            comment=comment,
            created_by="system",  # TODO: obtener del usuario autenticado
            created_at=now,
            updated_at=now,
        )
        db.add(adjustment)
        db.flush()

        # Crear entrada en ledger
        ledger_entry = FeedStockLedger(
            feed_type_id=feed_type_id,
            movement_type=FeedMovementType.adjustment,
            bags_delta=int(difference_bags or 0),
            kg_delta=Decimal("0"),  # No sabemos el kg exacto sin el peso_saco específico
            source_table="feed_inventory_adjustments",
            source_id=adjustment.id,
            created_at=now,
            created_by="system",
        )
        db.add(ledger_entry)

        # Crear evento para outbox
        outbox_event = AccountingOutboxEvent(
            event_type="feed_adjustment_created",
            payload_json=json.dumps({
                "feed_type_id": feed_type_id,
                "stock_calculated_bags": int(stock_calculated_bags or 0),
                "stock_real_bags": int(stock_real_bags),
                "difference_bags": int(difference_bags or 0),
                "comment": comment,
            }),
            status="pending",
            created_at=now,
        )
        db.add(outbox_event)

        db.commit()
        return JSONResponse({"success": True, "adjustment_id": adjustment.id})
    except Exception as e:
        db.rollback()
        return JSONResponse({"success": False, "error": f"Error al guardar: {str(e)}"}, status_code=500)


# ════════════════════════════════════════════════════════════════════════════════
# ENDPOINTS DE API (consultas)
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/ui/feed/new-program", response_class=HTMLResponse)
def ui_feed_new_program(
    request: Request,
    from_program: int = None,
    db: Session = Depends(get_db),
):
    """Vista de creacion de un nuevo programa de alimentacion."""
    feed_types = db.query(FeedType).filter(FeedType.active == 1).order_by(FeedType.name).all()

    stock_by_feed = {}
    for ft in feed_types:
        stock_by_feed[ft.id] = calculate_feed_stock(ft.id, db)

    # Precargar valores si viene de reprogramar
    prefill_map = {}  # (pond_id, ft_id) -> bags
    prefill_program = None
    if from_program:
        prefill_program = db.query(FeedProgram).filter(FeedProgram.id == from_program).first()
        if prefill_program:
            lines = db.query(FeedProgramLine).filter(
                FeedProgramLine.feed_program_id == from_program
            ).all()
            for line in lines:
                prefill_map[(int(line.pond_id), int(line.feed_type_id))] = int(line.planned_bags or 0)

    # Estanques padre con lotes activos (propios o de sus hijos)
    pond_rows_raw = (
        db.query(Pond, CultivationUnit)
        .outerjoin(CultivationUnit, CultivationUnit.id == Pond.cultivation_unit_id)
        .filter(Pond.parent_pond_id.is_(None))
        .order_by(
            func.coalesce(CultivationUnit.id, 999999),
            Pond.id,
        )
        .all()
    )

    # Pre-cargar todos los hijos de los padres
    parent_ids_np = [pond.id for pond, _ in pond_rows_raw]
    children_map_np = {}
    if parent_ids_np:
        all_children_np = db.query(Pond).filter(Pond.parent_pond_id.in_(parent_ids_np)).all()
        for child in all_children_np:
            if child.parent_pond_id not in children_map_np:
                children_map_np[child.parent_pond_id] = []
            children_map_np[child.parent_pond_id].append(child)

    # Recopilar todos los lotes
    lot_ids = set()
    for pond, _ in pond_rows_raw:
        if pond.active_lot_ids:
            for lid in pond.active_lot_ids:
                if lid:
                    lot_ids.add(int(lid))
        elif pond.lot_id:
            lot_ids.add(int(pond.lot_id))
        
        for child in children_map_np.get(pond.id, []):
            if child.active_lot_ids:
                for lid in child.active_lot_ids:
                    if lid:
                        lot_ids.add(int(lid))
            elif child.lot_id:
                lot_ids.add(int(child.lot_id))

    lot_map = {}
    if lot_ids:
        lots = db.query(Lot).filter(Lot.id.in_(list(lot_ids))).all()
        lot_map = {int(l.id): (l.internal_id or l.name) for l in lots}

    pond_rows = []
    for pond, cu in pond_rows_raw:
        # Recopilar nombres de lotes del padre + hijos
        lot_names = []
        
        if pond.active_lot_ids:
            for lid in pond.active_lot_ids:
                label = lot_map.get(int(lid))
                if label:
                    lot_names.append(label)
        elif pond.lot_id and int(pond.lot_id) in lot_map:
            lot_names.append(lot_map[int(pond.lot_id)])
        
        # Lotes de los hijos
        children = children_map_np.get(pond.id, [])
        for child in children:
            if child.active_lot_ids:
                for lid in child.active_lot_ids:
                    label = lot_map.get(int(lid))
                    if label and label not in lot_names:
                        lot_names.append(label)
            elif child.lot_id and int(child.lot_id) in lot_map:
                label = lot_map[int(child.lot_id)]
                if label not in lot_names:
                    lot_names.append(label)

        if not lot_names:
            continue

        feed_cells = []
        for ft in feed_types:
            # Suma: padre + todos los hijos
            prefill_bags = prefill_map.get((int(pond.id), int(ft.id)), 0)
            for child in children:
                prefill_bags += prefill_map.get((int(child.id), int(ft.id)), 0)
            
            feed_cells.append({
                "feed_type_id": ft.id,
                "prefill_bags": prefill_bags,
                "available_bags": stock_by_feed[ft.id]["available_bags"],
            })

        pond_rows.append({
            "pond_id": pond.id,
            "pond_name": pond.name,
            "cultivation_unit_name": cu.name if cu else "Sin unidad",
            "biomass_estimated": float(pond.biomass_current or pond.biomass or 0),
            "lot_names": lot_names,
            "feed_cells": feed_cells,
        })

    context = {
        "request": request,
        "feed_types": feed_types,
        "pond_rows": pond_rows,
        "stock_by_feed": stock_by_feed,
        "prefill_program": prefill_program,
        "msg": request.query_params.get("msg"),
        "status_msg": request.query_params.get("status", "ok"),
    }
    template = jinja_env.get_template("feed_new_program.html")
    return HTMLResponse(template.render(context))


@router.post("/ui/feed/new-program")
async def ui_feed_new_program_save(
    request: Request,
    db: Session = Depends(get_db),
):
    """Guarda un nuevo programa de alimentacion cerrando el anterior si existe."""
    form = await request.form()
    now = datetime.utcnow()

    feed_types = db.query(FeedType).filter(FeedType.active == 1).all()
    feed_type_ids = [ft.id for ft in feed_types]

    # Cerrar programa abierto anterior
    old_program = db.query(FeedProgram).filter(FeedProgram.status == "open").first()
    try:
        if old_program:
            # La planificacion no mueve stock fisico, asi que cerrar el
            # programa anterior no requiere liberar reservas en el ledger.
            old_program.status = "closed"
            old_program.closed_reason = "superseded"
            old_program.closed_at = now
            old_program.updated_at = now
            db.flush()

        # Crear nuevo programa
        day_code = f"PROG-{now.strftime('%Y%m%d')}"
        existing_codes = [
            row[0]
            for row in db.query(FeedProgram.program_code)
            .filter(FeedProgram.program_code.like(f"{day_code}%"))
            .all()
        ]

        if day_code not in existing_codes:
            code = day_code
        else:
            max_suffix = 1
            prefix = f"{day_code}-"
            for existing in existing_codes:
                if not existing.startswith(prefix):
                    continue
                suffix = existing[len(prefix):]
                # Solo consideramos sufijos cortos numericos (evita tomar timestamps viejos como secuencia)
                if suffix.isdigit() and len(suffix) <= 4:
                    max_suffix = max(max_suffix, int(suffix))

            code = f"{day_code}-{max_suffix + 1}"

        new_program = FeedProgram(
            program_code=code,
            week_start_date=now.date(),
            week_end_date=(now + timedelta(days=30)).date(),
            status="open",
            created_by="system",
            created_at=now,
            updated_at=now,
        )
        db.add(new_program)
        db.flush()

        # Procesar filas del formulario
        # Campos: feed_{pond_id}_{feed_type_id}
        pond_ids_seen = set()
        for key, val in form.multi_items():
            if not key.startswith("feed_"):
                continue
            parts = key.split("_")
            if len(parts) != 3:
                continue
            try:
                pond_id = int(parts[1])
                ft_id = int(parts[2])
                bags = int(val) if val not in (None, "") else 0
            except ValueError:
                continue

            if bags < 0:
                bags = 0

            pond_ids_seen.add(pond_id)

            line = FeedProgramLine(
                feed_program_id=new_program.id,
                pond_id=pond_id,
                feed_type_id=ft_id,
                planned_bags=bags,
                planned_kg=Decimal("0"),
                executed_bags=0,
                executed_kg=Decimal("0"),
                remaining_bags=bags,
                remaining_kg=Decimal("0"),
                created_at=now,
                updated_at=now,
            )
            db.add(line)
            db.flush()
            # Planificar no consume stock fisico: no se emite movimiento reserve.

        db.commit()
        return RedirectResponse(
            url=f"/views/ui/feed?msg=Programa+{code}+creado&status=ok",
            status_code=303,
        )
    except Exception as e:
        db.rollback()
        return RedirectResponse(
            url=f"/views/ui/feed/new-program?msg=Error+al+guardar&status=error",
            status_code=303,
        )


@router.get("/ui/feed/history", response_class=HTMLResponse)
def ui_feed_history(
    request: Request,
    date_from: str = None,
    date_to: str = None,
    db: Session = Depends(get_db),
):
    """Historial de programas cerrados con filtro por rango de fecha."""
    query = db.query(FeedProgram).filter(FeedProgram.status == "closed")

    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.filter(FeedProgram.week_start_date >= dt_from)
        except ValueError:
            pass

    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d")
            query = query.filter(FeedProgram.week_start_date <= dt_to)
        except ValueError:
            pass

    programs = query.order_by(FeedProgram.created_at.desc()).all()

    context = {
        "request": request,
        "programs": programs,
        "date_from": date_from or "",
        "date_to": date_to or "",
    }
    template = jinja_env.get_template("feed_history.html")
    return HTMLResponse(template.render(context))


@router.get("/ui/feed/history/{program_id}/reprogramar")
def ui_feed_reprogramar(
    program_id: int,
    db: Session = Depends(get_db),
):
    """Redirige a new-program con los valores del programa seleccionado precargados."""
    program = db.query(FeedProgram).filter(FeedProgram.id == program_id).first()
    if not program:
        return RedirectResponse(url="/views/ui/feed/history?msg=Programa+no+encontrado&status=error", status_code=303)
    return RedirectResponse(
        url=f"/views/ui/feed/new-program?from_program={program_id}",
        status_code=303,
    )

@router.get("/api/feed/program/{program_id}/detail")
def api_feed_program_detail(
    program_id: int,
    db: Session = Depends(get_db),
):
    """Retorna cabecera + líneas de un programa de alimentación como JSON."""
    program = db.query(FeedProgram).filter(FeedProgram.id == program_id).first()
    if not program:
        return JSONResponse({"error": "Programa no encontrado"}, status_code=404)

    rows = (
        db.query(FeedProgramLine, Pond, FeedType)
        .join(Pond, FeedProgramLine.pond_id == Pond.id)
        .join(FeedType, FeedProgramLine.feed_type_id == FeedType.id)
        .filter(FeedProgramLine.feed_program_id == program_id)
        .order_by(Pond.name, FeedType.name)
        .all()
    )

    return JSONResponse({
        "id": program.id,
        "program_code": program.program_code,
        "week_start_date": program.week_start_date.strftime("%d/%m/%Y") if program.week_start_date else None,
        "week_end_date": program.week_end_date.strftime("%d/%m/%Y") if program.week_end_date else None,
        "status": program.status,
        "closed_reason": program.closed_reason or "—",
        "created_by": program.created_by or "—",
        "created_at": program.created_at.strftime("%d/%m/%Y %H:%M") if program.created_at else None,
        "closed_at": program.closed_at.strftime("%d/%m/%Y %H:%M") if program.closed_at else None,
        "lines": [
            {
                "pond": pond.name,
                "feed_type": ft.name,
                "planned_bags": line.planned_bags,
                "planned_kg": float(line.planned_kg),
                "executed_bags": line.executed_bags,
                "executed_kg": float(line.executed_kg),
                "remaining_bags": line.remaining_bags,
                "remaining_kg": float(line.remaining_kg),
            }
            for line, pond, ft in rows
        ],
    })


@router.get("/api/feed/stock/{feed_type_id}")
def api_feed_stock(
    feed_type_id: int,
    db: Session = Depends(get_db)
):
    """Obtener stock actual para un tipo de alimento"""
    feed_type = db.query(FeedType).filter(FeedType.id == feed_type_id, FeedType.active == 1).first()
    if not feed_type:
        return JSONResponse({"success": False, "error": "Tipo de alimento no encontrado"}, status_code=404)

    stock = calculate_feed_stock(feed_type_id, db)

    return JSONResponse({
        "success": True,
        "data": {
            "feed_type_id": feed_type_id,
            "feed_type_name": feed_type.name,
            "calculated_total_bags": int(stock["available_bags"]),
            "available_bags": int(stock["available_bags"]),
            "planned_bags": int(stock["planned_bags"]),
            "executed_bags": int(stock["executed_bags"]),
            "total_kg": str(stock["total_kg"]),
        }
    })
