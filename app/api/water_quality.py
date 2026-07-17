"""Router del módulo de calidad de agua (páginas + ingreso).

Convención de CrianzaApp: páginas HTML bajo /views/ui/* renderizadas con
jinja_env + HTMLResponse, incluyendo _sidebar.html. Sin capa de auth.

Carga los umbrales de la BD (water_quality_thresholds) y los inyecta al motor
puro `app.services.water_quality`; persiste los flags/niveles calculados.
"""

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from sqlalchemy import func, and_
from sqlalchemy.orm import Session
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import quote_plus

from app.db.session import SessionLocal
from app.models.ponds import Pond
from app.models.cultivation_units import CultivationUnit
from app.models.users import User
from app.models.pond_oxygen_readings import PondOxygenReading
from app.models.biofilter_readings import BiofilterReading
from app.models.water_quality_thresholds import WaterQualityThreshold
from app.services import water_quality as wq

router = APIRouter(prefix="/views/ui/calidad-agua", tags=["calidad-agua"])
template_dir = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)))

# Una lectura de O2 se considera vencida si supera este intervalo (cada 2-4 h)
O2_STALE_HOURS = 4


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_decimal(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    value = str(value).strip().replace(",", ".")
    if value == "":
        return None
    try:
        return float(Decimal(value))
    except (InvalidOperation, ValueError):
        return None


def load_thresholds(db: Session) -> dict:
    """Lee los umbrales activos de la BD con la forma que espera el motor.

    Rellena con DEFAULT_THRESHOLDS cualquier parámetro faltante/inactivo.
    """
    out: dict[str, dict] = {}
    for r in db.query(WaterQualityThreshold).filter(WaterQualityThreshold.active.is_(True)).all():
        out[r.parameter] = {
            "alert": float(r.alert_value) if r.alert_value is not None else None,
            "alarm": float(r.alarm_value) if r.alarm_value is not None else None,
            "comparator": r.comparator,
        }
    for key, spec in wq.DEFAULT_THRESHOLDS.items():
        out.setdefault(key, spec)
    return out


def _active_users(db: Session):
    users = db.query(User).filter(User.active.is_(True)).order_by(User.name, User.lastname).all()
    return [{"id": u.id, "label": " ".join(x for x in [u.name, u.lastname] if x) or u.email} for u in users]


# ---------------------------------------------------------------------------
# Panel de estado
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel(request: Request, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        now = datetime.now()

        # Estanques padre activos con su unidad de cultivo
        ponds = (
            db.query(Pond)
            .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
            .order_by(Pond.name)
            .all()
        )
        unit_names = {u.id: u.name for u in db.query(CultivationUnit).all()}

        # Última lectura de O2 por estanque
        o2_sub = (
            db.query(
                PondOxygenReading.pond_id.label("pid"),
                func.max(PondOxygenReading.reading_datetime).label("mx"),
            )
            .group_by(PondOxygenReading.pond_id)
            .subquery()
        )
        o2_latest = {
            r.pond_id: r
            for r in db.query(PondOxygenReading).join(
                o2_sub,
                and_(
                    PondOxygenReading.pond_id == o2_sub.c.pid,
                    PondOxygenReading.reading_datetime == o2_sub.c.mx,
                ),
            ).all()
        }

        oxygen_rows = []
        for p in ponds:
            r = o2_latest.get(p.id)
            stale = False
            hours = None
            if r is not None and r.reading_datetime is not None:
                hours = (now - r.reading_datetime).total_seconds() / 3600.0
                stale = hours > O2_STALE_HOURS
            oxygen_rows.append({
                "pond_id": p.id,
                "pond_name": p.name,
                "unit_name": unit_names.get(p.cultivation_unit_id, "—"),
                "reading": r,
                "alarm_level": (r.alarm_level if r else None),
                "consistency_flag": (r.consistency_flag if r else None),
                "hours_ago": round(hours, 1) if hours is not None else None,
                "stale": stale,
                "has_data": r is not None,
            })

        # Última lectura de biofiltro por unidad de cultivo
        units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
        bf_sub = (
            db.query(
                BiofilterReading.cultivation_unit_id.label("uid"),
                func.max(BiofilterReading.reading_date).label("mx"),
            )
            .group_by(BiofilterReading.cultivation_unit_id)
            .subquery()
        )
        bf_latest = {}
        for r in db.query(BiofilterReading).join(
            bf_sub,
            and_(
                BiofilterReading.cultivation_unit_id == bf_sub.c.uid,
                BiofilterReading.reading_date == bf_sub.c.mx,
            ),
        ).order_by(BiofilterReading.id.desc()).all():
            bf_latest.setdefault(r.cultivation_unit_id, r)  # el más reciente por id si empatan fecha

        biofilter_rows = []
        for u in units:
            r = bf_latest.get(u.id)
            biofilter_rows.append({
                "unit_id": u.id,
                "unit_name": u.name,
                "reading": r,
                "alarm_level": (r.alarm_level if r else None),
                "n_balance_flag": (r.n_balance_flag if r else None),
                "ph_delta_flag": (r.ph_delta_flag if r else None),
                "temp_delta_flag": (r.temp_delta_flag if r else None),
                "has_data": r is not None,
            })

        def _count(rows, key="alarm_level"):
            return {
                "alarma": sum(1 for x in rows if x.get(key) == "alarma"),
                "alerta": sum(1 for x in rows if x.get(key) == "alerta"),
            }

        context = {
            "request": request,
            "msg": msg,
            "oxygen_rows": oxygen_rows,
            "biofilter_rows": biofilter_rows,
            "o2_counts": _count(oxygen_rows),
            "bf_counts": _count(biofilter_rows),
            "o2_stale_hours": O2_STALE_HOURS,
        }
        html = jinja_env.get_template("calidad_agua_panel.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Oxígeno: formulario + alta
# ---------------------------------------------------------------------------
@router.get("/oxigeno/nueva", response_class=HTMLResponse)
def oxigeno_form(request: Request, pond_id: Optional[int] = None):
    db = SessionLocal()
    try:
        ponds = (
            db.query(Pond)
            .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
            .order_by(Pond.name)
            .all()
        )
        context = {
            "request": request,
            "ponds": [{"id": p.id, "name": p.name} for p in ponds],
            "users": _active_users(db),
            "selected_pond": pond_id,
            "now_local": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "altitude_m": wq.SITE_ALTITUDE_M,
        }
        html = jinja_env.get_template("calidad_agua_oxigeno_form.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/oxigeno")
def oxigeno_create(
    pond_id: int = Form(...),
    reading_datetime: str = Form(...),
    do_mg_l: Optional[str] = Form(None),
    water_temp_c: Optional[str] = Form(None),
    saturation_pct: Optional[str] = Form(None),
    operator_id: Optional[str] = Form(None),
    observation: Optional[str] = Form(None),
):
    db = SessionLocal()
    try:
        try:
            rdt = datetime.fromisoformat(reading_datetime)
        except (ValueError, TypeError):
            rdt = datetime.now()

        do = _parse_decimal(do_mg_l)
        temp = _parse_decimal(water_temp_c)
        sat = _parse_decimal(saturation_pct)

        thresholds = load_thresholds(db)
        res = wq.evaluate_oxygen(do, temp, sat, thresholds=thresholds)

        reading = PondOxygenReading(
            pond_id=pond_id,
            operator_id=int(operator_id) if operator_id else None,
            reading_datetime=rdt,
            do_mg_l=do,
            water_temp_c=temp,
            saturation_pct=sat,
            saturation_computed_pct=res.saturation_computed_pct,
            consistency_flag=res.consistency_flag,
            alarm_level=res.alarm_level,
            observation=(observation or None),
            created_at=datetime.now(),
        )
        db.add(reading)
        db.commit()

        msg = f"Lectura de O2 registrada (estado: {res.alarm_level}"
        if res.consistency_flag == "sospechoso":
            msg += ", terna sospechosa"
        msg += ")."
        return RedirectResponse(url=f"/views/ui/calidad-agua?msg={quote_plus(msg)}", status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Biofiltro: formulario + alta
# ---------------------------------------------------------------------------
@router.get("/biofiltro/nueva", response_class=HTMLResponse)
def biofiltro_form(request: Request, unit_id: Optional[int] = None):
    db = SessionLocal()
    try:
        units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
        context = {
            "request": request,
            "units": [{"id": u.id, "name": u.name} for u in units],
            "users": _active_users(db),
            "selected_unit": unit_id,
            "today": date.today().strftime("%Y-%m-%d"),
        }
        html = jinja_env.get_template("calidad_agua_biofiltro_form.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/biofiltro")
def biofiltro_create(
    cultivation_unit_id: int = Form(...),
    reading_date: str = Form(...),
    operator_id: Optional[str] = Form(None),
    observation: Optional[str] = Form(None),
    in_ph: Optional[str] = Form(None),
    in_temp_c: Optional[str] = Form(None),
    in_nh4_n: Optional[str] = Form(None),
    in_no2_n: Optional[str] = Form(None),
    in_no3_n: Optional[str] = Form(None),
    out_ph: Optional[str] = Form(None),
    out_temp_c: Optional[str] = Form(None),
    out_nh4_n: Optional[str] = Form(None),
    out_no2_n: Optional[str] = Form(None),
    out_no3_n: Optional[str] = Form(None),
):
    db = SessionLocal()
    try:
        try:
            rdate = date.fromisoformat(reading_date)
        except (ValueError, TypeError):
            rdate = date.today()

        reading_vals = {
            "in_ph": _parse_decimal(in_ph),
            "in_temp_c": _parse_decimal(in_temp_c),
            "in_nh4_n": _parse_decimal(in_nh4_n),
            "in_no2_n": _parse_decimal(in_no2_n),
            "in_no3_n": _parse_decimal(in_no3_n),
            "out_ph": _parse_decimal(out_ph),
            "out_temp_c": _parse_decimal(out_temp_c),
            "out_nh4_n": _parse_decimal(out_nh4_n),
            "out_no2_n": _parse_decimal(out_no2_n),
            "out_no3_n": _parse_decimal(out_no3_n),
        }

        thresholds = load_thresholds(db)
        res = wq.evaluate_biofilter(reading_vals, thresholds=thresholds)

        reading = BiofilterReading(
            cultivation_unit_id=cultivation_unit_id,
            operator_id=int(operator_id) if operator_id else None,
            reading_date=rdate,
            tn_in=res.tn_in,
            tn_out=res.tn_out,
            nh3_n_in=(round(res.nh3_n_in, 4) if res.nh3_n_in is not None else None),
            nh3_n_out=(round(res.nh3_n_out, 4) if res.nh3_n_out is not None else None),
            n_balance_flag=res.n_balance_flag,
            ph_delta_flag=res.ph_delta_flag,
            temp_delta_flag=res.temp_delta_flag,
            alarm_level=res.alarm_level,
            observation=(observation or None),
            created_at=datetime.now(),
            **reading_vals,
        )
        db.add(reading)
        db.commit()

        flags = [f for f, v in (("balance N", res.n_balance_flag), ("ΔpH", res.ph_delta_flag),
                                ("Δtemp", res.temp_delta_flag)) if v == "sospechoso"]
        msg = f"Muestreo de biofiltro registrado (estado: {res.alarm_level}"
        if flags:
            msg += "; validación: " + ", ".join(flags)
        msg += ")."
        return RedirectResponse(url=f"/views/ui/calidad-agua?msg={quote_plus(msg)}", status_code=303)
    finally:
        db.close()
