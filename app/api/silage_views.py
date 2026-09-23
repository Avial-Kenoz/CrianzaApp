"""Vistas web del módulo de molienda y ensilaje (administración).

Convención de CrianzaApp: páginas HTML bajo /views/ui/* renderizadas con
jinja_env + HTMLResponse, incluyendo _sidebar.html. Sin capa de auth.

Cubre lo que la PWA no puede hacer y sin lo cual el módulo no se sostiene:
dar de alta **tambores vacíos** y registrar **ingresos de ácido**. La captura
diaria vive en la PWA (/ensilaje); acá está la trastienda.

El disponible de ácido siempre se CALCULA desde los movimientos
(ingresos + ajustes − consumos); no hay un campo "stock" que pueda quedar
desincronizado del histórico.
"""

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import quote_plus
import calendar
import csv
import io

from sqlalchemy import func, text

from app.db.session import SessionLocal
from app.models.users import User
from app.models.silage_drums import SilageDrum
from app.models.silage_drum_dispatches import SilageDrumDispatch
from app.models.silage_grinding_events import SilageGrindingEvent
from app.models.silage_weekly_inspections import SilageWeeklyInspection
from app.models.silage_acid import SilageAcidLot, SilageAcidMovement
from app.models.silage_thresholds import SilageThreshold
from app.services import silage as sg

router = APIRouter(prefix="/views/ui/ensilaje", tags=["ensilaje-vistas"])
template_dir = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)))

DRUM_STATUS_LABELS = {
    "available": "Vacío / disponible",
    "open": "Abierto (recibiendo)",
    "sealed": "Cerrado y sellado",
    "dispatched": "Retirado de planta",
}


def _parse_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    # OJO: no llamar `text` a esta variable; taparía el sqlalchemy.text importado
    # arriba y el fallo aparecería recién en otra función.
    raw = str(value).strip().replace(",", ".")
    if raw == "":
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _parse_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def load_thresholds(db) -> dict:
    out: dict = {}
    for row in db.query(SilageThreshold).filter(SilageThreshold.active.is_(True)).all():
        if row.value is not None:
            out[row.parameter] = float(row.value)
    for key, value in sg.DEFAULT_THRESHOLDS.items():
        out.setdefault(key, value)
    return out


def _redirect(path: str, msg: str, error: bool = False):
    """Redirige con el mensaje flash en la query.

    El separador se elige según la ruta: varias rutas ya traen query propia
    (`?semana=…`, o el `volver` de los formularios con `?mes=…`). Pegar siempre
    "?" generaba dos signos de interrogación y el mensaje se perdía dentro del
    valor del parámetro anterior, sin error visible.
    """
    key = "err" if error else "msg"
    sep = "&" if "?" in path else "?"
    return RedirectResponse(url=f"{path}{sep}{key}={quote_plus(msg)}", status_code=303)


def _month_bounds(mes: Optional[str]) -> tuple[date, date, str]:
    """(primer día, último día, 'YYYY-MM') del mes pedido; el actual si no viene
    o si llega mal formado."""
    today = date.today()
    try:
        year, month = (int(x) for x in (mes or "").split("-"))
        first = date(year, month, 1)
    except (ValueError, TypeError):
        first = today.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last, first.strftime("%Y-%m")


def _month_label(d: date) -> str:
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    return f"{meses[d.month - 1]} {d.year}"


DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def _events_between(db, desde: date, hasta: date):
    return (
        db.query(SilageGrindingEvent)
        .filter(SilageGrindingEvent.log_date >= desde, SilageGrindingEvent.log_date <= hasta)
        .order_by(SilageGrindingEvent.log_date, SilageGrindingEvent.event_datetime)
        .all()
    )


def _operator_names(db) -> dict:
    return {
        u.id: (" ".join(x for x in [u.name, u.lastname] if x) or u.email)
        for u in db.query(User).all()
    }


def _event_row(e: SilageGrindingEvent, operators: dict, ph_limit: float) -> dict:
    """Fila de la tabla de cargas: el registro oficial es esta lista, no la
    grilla del formato en papel."""
    ph = float(e.ph_value) if e.ph_value is not None else None
    return {
        "id": e.id,
        "folio": sg.format_folio(e.folio),
        "fecha": e.log_date,
        "dia": DIAS[e.log_date.weekday()] if e.log_date else "",
        "hora": e.event_datetime.strftime("%H:%M") if e.event_datetime else "",
        "had_mortality": e.had_mortality,
        "kg": float(e.mortality_kg) if e.mortality_kg is not None else None,
        "acid_lts": float(e.acid_lts) if e.acid_lts is not None else None,
        "additional_acid": e.additional_acid,
        "ph": ph,
        "ph_ok": (ph is not None and ph < ph_limit),
        "drum": e.drum_number,
        "drum_closed": e.drum_closed,
        "operador": operators.get(e.operator_id, "—"),
        "recibido_por": e.received_by_name,
        "recibido_at": e.received_at,
        "reception_note": e.reception_note,
        "observacion": e.observation,
        "voided": e.voided_at is not None,
        "void_reason": e.void_reason,
        "voided_by": e.voided_by,
    }


# ---------------------------------------------------------------------------
# Tambores
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
@router.get("/tambores", response_class=HTMLResponse)
def drums_page(request: Request, msg: str = None, err: str = None):
    db = SessionLocal()
    try:
        thresholds = load_thresholds(db)
        capacity = thresholds.get("drum_capacity_kg") or 0
        close_ratio = thresholds.get("drum_close_fill_ratio") or 0.8

        drums = (
            db.query(SilageDrum)
            .order_by(SilageDrum.status != "open", SilageDrum.drum_number)
            .all()
        )
        rows = []
        for d in drums:
            kg = float(d.total_kg or 0)
            fill_status, _ = sg.evaluate_drum_fill(d.total_kg, thresholds)
            rows.append({
                "id": d.id,
                "drum_number": d.drum_number,
                "status": d.status,
                "status_label": DRUM_STATUS_LABELS.get(d.status, d.status),
                "opened_at": d.opened_at,
                "sealed_at": d.sealed_at,
                "sealed_by": d.sealed_by,
                "seal_note": d.seal_note,
                "total_kg": kg,
                "acid_lts": float(d.acid_lts or 0),
                "pct": (kg / capacity * 100) if capacity else 0,
                "fill_status": fill_status,
                "observation": d.observation,
            })

        counts = {k: 0 for k in DRUM_STATUS_LABELS}
        for d in drums:
            counts[d.status] = counts.get(d.status, 0) + 1

        # Sugerencia del siguiente correlativo: el procedimiento manda seguir con
        # el tambor del número siguiente.
        max_number = max([d.drum_number for d in drums], default=0)

        context = {
            "request": request, "msg": msg, "err": err, "active": "tambores",
            "drums": rows, "counts": counts, "labels": DRUM_STATUS_LABELS,
            "capacity": capacity, "close_pct": int(close_ratio * 100),
            "next_number": max_number + 1,
            "open_drum": next((r for r in rows if r["status"] == "open"), None),
        }
        return HTMLResponse(jinja_env.get_template("ensilaje_tambores.html").render(context))
    finally:
        db.close()


@router.post("/tambores/nuevo")
async def drums_create(request: Request):
    """Alta de tambores vacíos. Acepta un rango para cargar la tanda completa de
    una vez (llegan de a varios, no de a uno)."""
    form = await request.form()
    desde = _parse_int(form.get("desde"))
    hasta = _parse_int(form.get("hasta")) or desde
    if desde is None or desde <= 0:
        return _redirect("/views/ui/ensilaje/tambores", "Indica el número de tambor", True)
    if hasta < desde:
        return _redirect("/views/ui/ensilaje/tambores",
                         "El número final no puede ser menor que el inicial", True)
    if hasta - desde > 200:
        return _redirect("/views/ui/ensilaje/tambores", "Rango demasiado grande (máx. 200)", True)

    db = SessionLocal()
    try:
        # No se recrean números que ya existen SIN retirar: el mismo rótulo se
        # reutiliza recién cuando el tambor anterior salió de planta.
        busy = {
            n for (n,) in db.query(SilageDrum.drum_number)
            .filter(SilageDrum.drum_number.in_(range(desde, hasta + 1)),
                    SilageDrum.status.in_(["available", "open", "sealed"]))
            .all()
        }
        created = 0
        for number in range(desde, hasta + 1):
            if number in busy:
                continue
            db.add(SilageDrum(drum_number=number, status="available",
                              total_kg=0, acid_lts=0,
                              observation=(form.get("observacion") or None),
                              created_at=datetime.now()))
            created += 1
        db.commit()
        skipped = (hasta - desde + 1) - created
        msg = f"{created} tambor(es) dado(s) de alta."
        if skipped:
            msg += f" {skipped} ya existía(n) sin retirar y se omitieron."
        return _redirect("/views/ui/ensilaje/tambores", msg)
    finally:
        db.close()


@router.post("/tambores/{drum_id}/estado")
async def drums_set_status(drum_id: int, request: Request):
    """Cambio manual de estado (cerrar un tambor olvidado, dar de baja, corregir)."""
    form = await request.form()
    new_status = (form.get("status") or "").strip()
    if new_status not in DRUM_STATUS_LABELS:
        return _redirect("/views/ui/ensilaje/tambores", "Estado inválido", True)

    db = SessionLocal()
    try:
        drum = db.get(SilageDrum, drum_id)
        if drum is None:
            return _redirect("/views/ui/ensilaje/tambores", "Tambor no encontrado", True)

        # La BD garantiza un solo tambor abierto (índice único parcial); se avisa
        # antes en vez de dejar reventar el IntegrityError en la cara del usuario.
        if new_status == "open" and drum.status != "open":
            other = db.query(SilageDrum).filter(SilageDrum.status == "open").first()
            if other is not None:
                return _redirect(
                    "/views/ui/ensilaje/tambores",
                    f"Ya hay un tambor abierto (N° {other.drum_number}); ciérralo primero", True)

        drum.status = new_status
        if new_status == "open" and not drum.opened_at:
            drum.opened_at = date.today()
        if new_status == "sealed" and not drum.sealed_at:
            drum.sealed_at = date.today()
        if new_status == "dispatched" and not drum.dispatched_at:
            drum.dispatched_at = date.today()
        drum.updated_at = datetime.now()
        db.commit()
        return _redirect("/views/ui/ensilaje/tambores",
                         f"Tambor N° {drum.drum_number}: {DRUM_STATUS_LABELS[new_status]}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Ácido
# ---------------------------------------------------------------------------
@router.get("/acido", response_class=HTMLResponse)
def acid_page(request: Request, msg: str = None, err: str = None):
    db = SessionLocal()
    try:
        thresholds = load_thresholds(db)
        lots = db.query(SilageAcidLot).order_by(SilageAcidLot.received_date.desc().nullslast(),
                                                SilageAcidLot.id.desc()).all()
        movements = (
            db.query(SilageAcidMovement)
            .order_by(SilageAcidMovement.movement_date.desc(), SilageAcidMovement.id.desc())
            .limit(60).all()
        )
        all_movements = db.query(SilageAcidMovement.movement_type, SilageAcidMovement.lts).all()
        available = sg.acid_available_lts(
            [{"movement_type": t, "lts": l} for t, l in all_movements]
        )
        low = sg.acid_stock_is_low(available, thresholds)

        lot_names = {l.id: l.lot_code for l in lots}
        totals = {"in": Decimal(0), "out": Decimal(0), "adjustment": Decimal(0)}
        for t, l in all_movements:
            totals[t] = totals.get(t, Decimal(0)) + (l or Decimal(0))

        context = {
            "request": request, "msg": msg, "err": err, "active": "acido",
            "lots": [{
                "id": l.id, "lot_code": l.lot_code, "supplier": l.supplier,
                "received_date": l.received_date,
                "received_lts": float(l.received_lts or 0),
                "concentration": l.concentration, "active": l.active,
            } for l in lots],
            "movements": [{
                "id": m.id, "date": m.movement_date, "type": m.movement_type,
                "lts": float(m.lts or 0), "lot": lot_names.get(m.lot_id, "—"),
                "notes": m.notes, "event_id": m.grinding_event_id,
            } for m in movements],
            "available": float(available),
            "low": low,
            "low_threshold": thresholds.get("acid_low_stock_lts"),
            "nominal": thresholds.get("acid_lts_per_kg_nominal"),
            "totals": {k: float(v) for k, v in totals.items()},
            "today": date.today().isoformat(),
        }
        return HTMLResponse(jinja_env.get_template("ensilaje_acido.html").render(context))
    finally:
        db.close()


@router.post("/acido/lote")
async def acid_lot_create(request: Request):
    """Ingreso de un lote de ácido. Crea el lote y su movimiento `in` en la misma
    operación: un lote recibido sin movimiento no sumaría al disponible."""
    form = await request.form()
    lot_code = (form.get("lot_code") or "").strip()
    lts = _parse_decimal(form.get("received_lts"))
    if not lot_code:
        return _redirect("/views/ui/ensilaje/acido", "Indica el código del lote", True)
    if lts is None or lts <= 0:
        return _redirect("/views/ui/ensilaje/acido", "Indica los litros recibidos", True)

    db = SessionLocal()
    try:
        if db.query(SilageAcidLot).filter(SilageAcidLot.lot_code == lot_code).first():
            return _redirect("/views/ui/ensilaje/acido",
                             f"El lote «{lot_code}» ya está registrado", True)
        received = form.get("received_date") or None
        received_date = date.fromisoformat(received) if received else date.today()

        lot = SilageAcidLot(
            lot_code=lot_code,
            supplier=(form.get("supplier") or None),
            received_date=received_date,
            received_lts=lts,
            concentration=(form.get("concentration") or None),
            notes=(form.get("notes") or None),
            active=True,
            created_at=datetime.now(),
        )
        db.add(lot)
        db.flush()
        db.add(SilageAcidMovement(
            lot_id=lot.id, movement_date=received_date, movement_type="in", lts=lts,
            notes=f"Ingreso de lote {lot_code}", created_at=datetime.now(),
        ))
        db.commit()
        return _redirect("/views/ui/ensilaje/acido",
                         f"Lote «{lot_code}» ingresado: {lts} lts.")
    finally:
        db.close()


@router.post("/acido/ajuste")
async def acid_adjustment(request: Request):
    """Ajuste manual del disponible (mermas, derrames, correcciones de conteo).

    Va como movimiento, no como edición del stock: el disponible se sigue
    calculando y el ajuste queda con su motivo en el histórico.
    """
    form = await request.form()
    lts = _parse_decimal(form.get("lts"))
    reason = (form.get("reason") or "").strip()
    if lts is None or lts == 0:
        return _redirect("/views/ui/ensilaje/acido",
                         "Indica los litros del ajuste (negativo para descontar)", True)
    if not reason:
        return _redirect("/views/ui/ensilaje/acido", "El ajuste requiere un motivo", True)

    db = SessionLocal()
    try:
        db.add(SilageAcidMovement(
            lot_id=_parse_int(form.get("lot_id")),
            movement_date=date.today(),
            movement_type="adjustment",
            lts=lts,
            notes=reason,
            created_at=datetime.now(),
        ))
        db.commit()
        signo = "sumaron" if lts > 0 else "descontaron"
        return _redirect("/views/ui/ensilaje/acido", f"Ajuste registrado: se {signo} {abs(lts)} lts.")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Registro oficial: tabla de cargas + resumen del mes
# ---------------------------------------------------------------------------
@router.get("/registro", response_class=HTMLResponse)
def registro_page(request: Request, mes: str = None, msg: str = None, err: str = None):
    """El registro oficial es la **tabla de cargas**, una línea por carga.

    No se replica la grilla del formato en papel: un día con dos tambores son dos
    líneas, y así cada una lleva su propio pH, tambor y folio sin ambigüedad. El
    resumen por día que va arriba es información de gestión, no el documento.
    """
    db = SessionLocal()
    try:
        first, last, mes_key = _month_bounds(mes)
        thresholds = load_thresholds(db)
        ph_limit = thresholds.get("ph_limit", 4.0)
        events = _events_between(db, first, last)
        operators = _operator_names(db)

        rows = [_event_row(e, operators, ph_limit) for e in events]
        summaries = sg.build_daily_summaries(events, thresholds)

        # Un día del mes SIN ninguna fila es un registro faltante; uno declarado
        # sin mortalidad no lo es. El papel distingue la X de la celda en blanco.
        today = date.today()
        dias = []
        d = first
        while d <= last:
            s = summaries.get(d)
            if d <= today:
                dias.append({
                    "fecha": d,
                    "dia": DIAS[d.weekday()],
                    "kg": float(s.total_kg) if s else 0.0,
                    "cargas": s.events_count if s else 0,
                    "acido": float(s.total_acid_lts) if s else 0.0,
                    "tambores": s.drums if s else [],
                    "ph_min": float(s.ph_min) if s and s.ph_min is not None else None,
                    "ph_max": float(s.ph_max) if s and s.ph_max is not None else None,
                    "no_conformes": s.non_conforming_count if s else 0,
                    "sin_mortalidad": s.no_mortality_declared if s else False,
                    "pendientes": s.pending_reception if s else 0,
                    "anuladas": s.voided_count if s else 0,
                    "falta": (s is None) or s.is_missing_record,
                })
            d += timedelta(days=1)

        vivos = [r for r in rows if not r["voided"]]
        total_kg = sum(r["kg"] or 0 for r in vivos if r["had_mortality"])
        total_acido = sum(r["acid_lts"] or 0 for r in vivos)
        context = {
            "request": request, "msg": msg, "err": err, "active": "registro",
            "mes": mes_key, "mes_label": _month_label(first),
            "mes_prev": (first - timedelta(days=1)).strftime("%Y-%m"),
            "mes_next": (last + timedelta(days=1)).strftime("%Y-%m"),
            "rows": rows, "dias": dias,
            "total_kg": total_kg, "total_acido": total_acido,
            "total_cargas": len([r for r in vivos if r["had_mortality"]]),
            "pendientes": len([r for r in vivos if r["had_mortality"] and not r["recibido_por"]]),
            "no_conformes": len([r for r in vivos if r["ph"] is not None and not r["ph_ok"]]),
            "faltantes": len([x for x in dias if x["falta"]]),
            "ph_limit": ph_limit,
            "hoy": today,
        }
        return HTMLResponse(jinja_env.get_template("ensilaje_registro.html").render(context))
    finally:
        db.close()


@router.get("/registro.csv")
def registro_csv(mes: str = None):
    """Export del mes. Una fila por carga, los mismos datos que la tabla."""
    db = SessionLocal()
    try:
        first, last, mes_key = _month_bounds(mes)
        thresholds = load_thresholds(db)
        ph_limit = thresholds.get("ph_limit", 4.0)
        operators = _operator_names(db)
        rows = [_event_row(e, operators, ph_limit) for e in _events_between(db, first, last)]

        def dec(value):
            return "" if value is None else ("%.2f" % value).replace(".", ",")

        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Folio", "Fecha", "Dia", "Hora", "Kg mortalidad", "Acido lts",
                    "Acido adicional", "pH", "pH conforme", "Tambor", "Cierre tambor",
                    "Operador", "Recibido por", "Recibido el", "Observacion",
                    "Anulada", "Motivo anulacion"])
        for r in rows:
            w.writerow([
                r["folio"],
                r["fecha"].strftime("%d-%m-%Y") if r["fecha"] else "",
                r["dia"], r["hora"], dec(r["kg"]), dec(r["acid_lts"]),
                "Si" if r["additional_acid"] else "",
                dec(r["ph"]),
                ("" if r["ph"] is None else ("Si" if r["ph_ok"] else "NO")),
                r["drum"] or "", "Si" if r["drum_closed"] else "",
                r["operador"], r["recibido_por"] or "",
                r["recibido_at"].strftime("%d-%m-%Y %H:%M") if r["recibido_at"] else "",
                r["observacion"] or "",
                "Si" if r["voided"] else "", r["void_reason"] or "",
            ])
        # BOM: Excel en español solo abre bien los acentos del CSV si lo lleva.
        data = "﻿" + buf.getvalue()
        return PlainTextResponse(
            data, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="ensilaje_%s.csv"' % mes_key},
        )
    finally:
        db.close()


@router.get("/dia/{fecha}", response_class=HTMLResponse)
def dia_page(request: Request, fecha: str, msg: str = None, err: str = None):
    """Detalle de un día: sus cargas, incluidas las anuladas (tachadas y con su
    motivo), que es donde está el valor de auditoría."""
    db = SessionLocal()
    try:
        try:
            day = date.fromisoformat(fecha)
        except ValueError:
            return _redirect("/views/ui/ensilaje/registro", "Fecha inválida", True)

        thresholds = load_thresholds(db)
        ph_limit = thresholds.get("ph_limit", 4.0)
        operators = _operator_names(db)
        events = _events_between(db, day, day)
        rows = [_event_row(e, operators, ph_limit) for e in events]
        summary = sg.build_daily_summary(events, thresholds, log_date=day)

        context = {
            "request": request, "msg": msg, "err": err, "active": "registro",
            "fecha": day, "dia": DIAS[day.weekday()],
            "rows": rows,
            "total_kg": float(summary.total_kg),
            "total_acido": float(summary.total_acid_lts),
            "cargas": summary.events_count,
            "anuladas": summary.voided_count,
            "tambores": summary.drums,
            "sin_mortalidad": summary.no_mortality_declared,
            "falta": summary.is_missing_record,
            "pendientes": summary.pending_reception,
            "ph_limit": ph_limit,
            "prev": (day - timedelta(days=1)).isoformat(),
            "next": (day + timedelta(days=1)).isoformat(),
            "mes": day.strftime("%Y-%m"),
        }
        return HTMLResponse(jinja_env.get_template("ensilaje_dia.html").render(context))
    finally:
        db.close()


@router.post("/registro/{event_id}/recibir")
async def registro_recibir(event_id: int, request: Request):
    """Recepción por un segundo usuario.

    OJO: `received_by_name` es TEXTO ESCRITO. CrianzaApp no tiene autenticación,
    así que no acredita identidad y no debe presentarse como firma.
    """
    form = await request.form()
    nombre = (form.get("received_by_name") or "").strip()
    volver = form.get("volver") or "/views/ui/ensilaje/registro"
    if not nombre:
        return _redirect(volver, "Indica quién recibe", True)

    db = SessionLocal()
    try:
        event = db.get(SilageGrindingEvent, event_id)
        if event is None:
            return _redirect(volver, "Carga no encontrada", True)
        if event.voided_at is not None:
            return _redirect(volver, "La carga está anulada", True)
        if event.received_at is not None:
            # Idempotente: no se pisa la recepción original.
            return _redirect(volver, "Ya estaba recibida por %s" % event.received_by_name)

        event.received_by_name = nombre[:120]
        event.received_at = datetime.now()
        event.reception_note = (form.get("reception_note") or None)
        event.updated_at = datetime.now()
        db.commit()
        return _redirect(volver, "Carga %s recibida por %s." % (sg.format_folio(event.folio), nombre))
    finally:
        db.close()


@router.post("/registro/{event_id}/anular")
async def registro_anular(event_id: int, request: Request):
    """Anulación: la única forma de corregir, porque la captura es aditiva.

    Revierte los efectos de inventario (kg del tambor y ácido) pero NO borra la
    carga: queda tachada con su motivo.
    """
    form = await request.form()
    motivo = (form.get("void_reason") or "").strip()
    volver = form.get("volver") or "/views/ui/ensilaje/registro"
    if not motivo:
        return _redirect(volver, "La anulación requiere un motivo", True)

    db = SessionLocal()
    try:
        event = db.get(SilageGrindingEvent, event_id)
        if event is None:
            return _redirect(volver, "Carga no encontrada", True)
        if event.voided_at is not None:
            return _redirect(volver, "La carga ya estaba anulada", True)

        event.voided_at = datetime.now()
        event.voided_by = (form.get("voided_by") or None)
        event.void_reason = motivo
        event.updated_at = datetime.now()

        drum = db.get(SilageDrum, event.drum_id) if event.drum_id else None
        if drum is not None:
            drum.total_kg = (drum.total_kg or Decimal(0)) - (event.mortality_kg or Decimal(0))
            drum.acid_lts = (drum.acid_lts or Decimal(0)) - (event.acid_lts or Decimal(0))
            drum.updated_at = datetime.now()
        if event.acid_lts and event.acid_lts > 0:
            db.add(SilageAcidMovement(
                movement_date=date.today(), movement_type="adjustment", lts=event.acid_lts,
                grinding_event_id=event.id, drum_id=event.drum_id,
                notes="Reverso por anulación de carga %s" % sg.format_folio(event.folio),
                created_at=datetime.now(),
            ))
        db.commit()
        return _redirect(volver, "Carga %s anulada." % sg.format_folio(event.folio))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Inspección semanal
# ---------------------------------------------------------------------------
@router.get("/inspeccion", response_class=HTMLResponse)
def inspeccion_page(request: Request, semana: str = None, msg: str = None, err: str = None):
    db = SessionLocal()
    try:
        try:
            ref = date.fromisoformat(semana) if semana else date.today()
        except ValueError:
            ref = date.today()
        week_start = ref - timedelta(days=ref.weekday())
        week_end = week_start + timedelta(days=6)

        row = (db.query(SilageWeeklyInspection)
               .filter(SilageWeeklyInspection.week_start == week_start).first())
        events = _events_between(db, week_start, week_end)
        totals = sg.build_period_totals(events)
        disponible = sg.acid_available_lts([
            {"movement_type": t, "lts": l}
            for t, l in db.query(SilageAcidMovement.movement_type, SilageAcidMovement.lts).all()
        ])

        # Conteos que el sistema puede calcular por su cuenta, para contrastar.
        drums_sealed_now = db.query(func.count(SilageDrum.id)).filter(
            SilageDrum.status == "sealed").scalar() or 0
        drums_available_now = db.query(func.count(SilageDrum.id)).filter(
            SilageDrum.status == "available").scalar() or 0

        variances = []
        if row is not None:
            variances = [{
                "item": v.item,
                "declared": float(v.declared) if v.declared is not None else None,
                "computed": float(v.computed) if v.computed is not None else None,
                "delta": float(v.delta) if v.delta is not None else None,
                "matches": v.matches,
            } for v in sg.compare_weekly_declaration(row, totals, disponible)]

        history = (db.query(SilageWeeklyInspection)
                   .order_by(SilageWeeklyInspection.week_start.desc()).limit(12).all())

        context = {
            "request": request, "msg": msg, "err": err, "active": "inspeccion",
            "week_start": week_start, "week_end": week_end,
            "prev": (week_start - timedelta(days=7)).isoformat(),
            "next": (week_start + timedelta(days=7)).isoformat(),
            "row": row,
            "computed": {
                "kg": float(totals.total_kg), "acido": float(totals.total_acid_lts),
                "cargas": totals.events_count, "tambores_usados": totals.drums_used,
                "tambores_sellados": totals.drums_sealed,
                "disponible": float(disponible),
                "sellados_ahora": drums_sealed_now, "vacios_ahora": drums_available_now,
            },
            "variances": variances,
            "lots": [l.lot_code for l in db.query(SilageAcidLot)
                     .filter(SilageAcidLot.active.is_(True)).order_by(SilageAcidLot.lot_code).all()],
            "history": [{
                "week_start": h.week_start,
                "acido": float(h.acid_used_lts or 0),
                "disponible": float(h.acid_available_lts or 0),
                "tambores": h.drums_used,
                "responsable": h.responsible_name, "inspector": h.inspector_name,
                "limpieza": h.cleanliness_ok,
            } for h in history],
        }
        return HTMLResponse(jinja_env.get_template("ensilaje_inspeccion.html").render(context))
    finally:
        db.close()


@router.post("/inspeccion")
async def inspeccion_save(request: Request):
    form = await request.form()
    try:
        week_start = date.fromisoformat(form.get("week_start"))
    except (ValueError, TypeError):
        return _redirect("/views/ui/ensilaje/inspeccion", "Semana inválida", True)
    week_start = week_start - timedelta(days=week_start.weekday())

    db = SessionLocal()
    try:
        row = (db.query(SilageWeeklyInspection)
               .filter(SilageWeeklyInspection.week_start == week_start).first())
        nuevo = row is None
        if nuevo:
            row = SilageWeeklyInspection(week_start=week_start, created_at=datetime.now())
            db.add(row)

        # Lo declarado se guarda TAL CUAL: es el registro con valor legal. El
        # sistema calcula lo suyo aparte y muestra el descuadre, sin corregirlo.
        row.cleanliness_ok = form.get("cleanliness_ok") is not None
        row.drums_sealed_ok = form.get("drums_sealed_ok") is not None
        row.drums_sealed_count = _parse_int(form.get("drums_sealed_count"))
        row.acid_used_lts = _parse_decimal(form.get("acid_used_lts"))
        row.acid_available_lts = _parse_decimal(form.get("acid_available_lts"))
        row.drums_used = _parse_int(form.get("drums_used"))
        row.drums_available = _parse_int(form.get("drums_available"))
        row.acid_lot = (form.get("acid_lot") or None)
        row.responsible_name = (form.get("responsible_name") or None)
        row.inspector_name = (form.get("inspector_name") or None)
        row.observation = (form.get("observation") or None)
        row.inspected_at = datetime.now()
        row.updated_at = datetime.now()
        db.commit()
        verbo = "registrada" if nuevo else "actualizada"
        return _redirect("/views/ui/ensilaje/inspeccion?semana=%s" % week_start.isoformat(),
                         "Inspección de la semana del %s %s." % (week_start.strftime("%d-%m-%Y"), verbo))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Despachos: guía + baja de tambores
# ---------------------------------------------------------------------------
@router.get("/despachos", response_class=HTMLResponse)
def despachos_page(request: Request, msg: str = None, err: str = None):
    db = SessionLocal()
    try:
        sealed = (db.query(SilageDrum).filter(SilageDrum.status == "sealed")
                  .order_by(SilageDrum.drum_number).all())
        dispatches = (db.query(SilageDrumDispatch)
                      .order_by(SilageDrumDispatch.dispatch_date.desc(),
                                SilageDrumDispatch.id.desc()).limit(30).all())
        drums_by_dispatch: dict = {}
        for d in db.query(SilageDrum).filter(SilageDrum.dispatch_id.isnot(None)).all():
            drums_by_dispatch.setdefault(d.dispatch_id, []).append(d.drum_number)

        context = {
            "request": request, "msg": msg, "err": err, "active": "despachos",
            "sealed": [{"id": d.id, "drum_number": d.drum_number,
                        "sealed_at": d.sealed_at, "total_kg": float(d.total_kg or 0)}
                       for d in sealed],
            "sealed_kg": sum(float(d.total_kg or 0) for d in sealed),
            "dispatches": [{
                "id": x.id, "fecha": x.dispatch_date, "guia": x.guide_number,
                "carrier": x.carrier, "destino": x.destination,
                "tambores": x.total_drums, "kg": float(x.total_kg or 0),
                "numeros": sorted(drums_by_dispatch.get(x.id, [])),
                "notas": x.notes,
            } for x in dispatches],
            "today": date.today().isoformat(),
        }
        return HTMLResponse(jinja_env.get_template("ensilaje_despachos.html").render(context))
    finally:
        db.close()


@router.post("/despachos/nuevo")
async def despacho_create(request: Request):
    """Emite la guía y da de baja los tambores incluidos.

    Solo se despachan tambores **sellados**: uno abierto sigue recibiendo cargas,
    y sacarlo de planta dejaría el registro del día apuntando a un tambor que ya
    no está.
    """
    form = await request.form()
    seleccion = form.getlist("drum_ids")
    if not seleccion:
        return _redirect("/views/ui/ensilaje/despachos", "Selecciona al menos un tambor", True)
    try:
        fecha = date.fromisoformat(form.get("dispatch_date") or date.today().isoformat())
    except ValueError:
        fecha = date.today()

    db = SessionLocal()
    try:
        ids = [int(x) for x in seleccion if str(x).isdigit()]
        drums = db.query(SilageDrum).filter(SilageDrum.id.in_(ids)).all()
        no_sellados = [str(d.drum_number) for d in drums if d.status != "sealed"]
        if no_sellados:
            return _redirect("/views/ui/ensilaje/despachos",
                             "Estos tambores no están sellados: %s" % ", ".join(no_sellados), True)
        if not drums:
            return _redirect("/views/ui/ensilaje/despachos", "No se encontraron los tambores", True)

        dispatch = SilageDrumDispatch(
            dispatch_date=fecha,
            guide_number=(form.get("guide_number") or None),
            carrier=(form.get("carrier") or None),
            destination=(form.get("destination") or None),
            total_drums=len(drums),
            total_kg=sum((d.total_kg or Decimal(0)) for d in drums),
            notes=(form.get("notes") or None),
            created_at=datetime.now(),
        )
        db.add(dispatch)
        db.flush()
        for d in drums:
            d.status = "dispatched"
            d.dispatched_at = fecha
            d.dispatch_id = dispatch.id
            d.updated_at = datetime.now()
        db.commit()
        return _redirect("/views/ui/ensilaje/despachos",
                         "Guía emitida: %d tambor(es) dados de baja (%.1f kg)."
                         % (len(drums), float(dispatch.total_kg)))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Conciliación: kg molidos vs mortalidad declarada en CrianzaApp
# ---------------------------------------------------------------------------
@router.get("/conciliacion", response_class=HTMLResponse)
def conciliacion_page(request: Request, mes: str = None):
    """Contrasta los kg molidos contra los peces muertos declarados en cultivo.

    Es **informativo y no bloqueante** a propósito: los dos registros se llenan
    en momentos y por personas distintas, así que un descuadre puntual es normal
    (un pez muerto el domingo se muele el lunes). Lo que importa es la tendencia
    del peso medio y los días con molienda sin ninguna mortalidad declarada.

    La mortalidad se lee de `ponds_movements` con `movement_reason='mortality'`,
    que es donde la registra el resto de la app (las tablas `mortality_reports`
    están prácticamente en desuso).
    """
    db = SessionLocal()
    try:
        first, last, mes_key = _month_bounds(mes)
        thresholds = load_thresholds(db)
        events = _events_between(db, first, last)
        summaries = sg.build_daily_summaries(events, thresholds)

        muertes = dict(db.execute(text(
            "SELECT movement_time::date AS d, COALESCE(SUM(fish_quantity), 0) "
            "FROM ponds_movements "
            "WHERE movement_reason = 'mortality' "
            "  AND movement_time::date BETWEEN :a AND :b "
            "GROUP BY 1"
        ), {"a": first, "b": last}).all())

        filas = []
        today = date.today()
        d = first
        while d <= last and d <= today:
            s = summaries.get(d)
            kg = float(s.total_kg) if s else 0.0
            peces = int(muertes.get(d, 0))
            filas.append({
                "fecha": d, "dia": DIAS[d.weekday()],
                "kg": kg, "peces": peces,
                "kg_por_pez": (kg / peces) if peces else None,
                # Los dos casos que vale la pena mirar:
                "molio_sin_declarar": kg > 0 and peces == 0,
                "declaro_sin_moler": peces > 0 and kg == 0,
            })
            d += timedelta(days=1)

        tot_kg = sum(f["kg"] for f in filas)
        tot_peces = sum(f["peces"] for f in filas)
        context = {
            "request": request, "active": "conciliacion",
            "mes": mes_key, "mes_label": _month_label(first),
            "mes_prev": (first - timedelta(days=1)).strftime("%Y-%m"),
            "mes_next": (last + timedelta(days=1)).strftime("%Y-%m"),
            "filas": filas,
            "total_kg": tot_kg, "total_peces": tot_peces,
            "kg_por_pez": (tot_kg / tot_peces) if tot_peces else None,
            "sin_declarar": len([f for f in filas if f["molio_sin_declarar"]]),
            "sin_moler": len([f for f in filas if f["declaro_sin_moler"]]),
        }
        return HTMLResponse(jinja_env.get_template("ensilaje_conciliacion.html").render(context))
    finally:
        db.close()
