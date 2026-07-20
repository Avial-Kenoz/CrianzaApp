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
import secrets
import segno

from app.db.session import SessionLocal
from app.models.ponds import Pond
from app.models.cultivation_units import CultivationUnit
from app.models.users import User
from app.models.pond_oxygen_readings import PondOxygenReading
from app.models.biofilter_readings import BiofilterReading
from app.models.water_quality_thresholds import WaterQualityThreshold
from app.models.water_quality_test_specs import WaterQualityTestSpec
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


def load_test_specs(db: Session) -> dict:
    """Lee las especificaciones de los tests de N con la forma que espera el
    motor. Rellena con DEFAULT_TEST_SPECS lo que falte."""
    out: dict[str, dict] = {}
    for r in db.query(WaterQualityTestSpec).all():
        out[r.test] = {
            "resolution": float(r.resolution) if r.resolution is not None else None,
            "acc_fixed": float(r.acc_fixed) if r.acc_fixed is not None else 0.0,
            "acc_pct": float(r.acc_pct) if r.acc_pct is not None else 0.0,
        }
    for key, spec in wq.DEFAULT_TEST_SPECS.items():
        out.setdefault(key, spec)
    return out


def _active_users(db: Session):
    users = db.query(User).filter(User.active.is_(True)).order_by(User.name, User.lastname).all()
    return [{"id": u.id, "label": " ".join(x for x in [u.name, u.lastname] if x) or u.email} for u in users]


# ---------------------------------------------------------------------------
# Helpers de última lectura
# ---------------------------------------------------------------------------
def _latest_o2_by_pond(db: Session) -> dict:
    """Última lectura de O2 por estanque (pond_id -> PondOxygenReading)."""
    sub = (
        db.query(
            PondOxygenReading.pond_id.label("pid"),
            func.max(PondOxygenReading.reading_datetime).label("mx"),
        )
        .group_by(PondOxygenReading.pond_id)
        .subquery()
    )
    out = {}
    for r in db.query(PondOxygenReading).join(
        sub, and_(PondOxygenReading.pond_id == sub.c.pid,
                  PondOxygenReading.reading_datetime == sub.c.mx),
    ).order_by(PondOxygenReading.id.desc()).all():
        out.setdefault(r.pond_id, r)
    return out


def _latest_bf_by_unit(db: Session) -> dict:
    """Último muestreo de biofiltro por unidad (unit_id -> BiofilterReading)."""
    sub = (
        db.query(
            BiofilterReading.cultivation_unit_id.label("uid"),
            func.max(BiofilterReading.reading_date).label("mx"),
        )
        .group_by(BiofilterReading.cultivation_unit_id)
        .subquery()
    )
    out = {}
    for r in db.query(BiofilterReading).join(
        sub, and_(BiofilterReading.cultivation_unit_id == sub.c.uid,
                  BiofilterReading.reading_date == sub.c.mx),
    ).order_by(BiofilterReading.id.desc()).all():
        out.setdefault(r.cultivation_unit_id, r)
    return out


def _o2_hours_ago(reading, now: datetime):
    if reading is None or reading.reading_datetime is None:
        return None, False
    hours = (now - reading.reading_datetime).total_seconds() / 3600.0
    return round(hours, 1), hours > O2_STALE_HOURS


# ---------------------------------------------------------------------------
# Panel de estado (rollup por unidad de cultivo, semáforo + detalle inline)
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel(request: Request, msg: Optional[str] = None, open: Optional[int] = None):
    db = SessionLocal()
    try:
        now = datetime.now()
        units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
        ponds = (
            db.query(Pond)
            .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
            .order_by(Pond.name)
            .all()
        )
        ponds_by_unit: dict = {}
        for p in ponds:
            ponds_by_unit.setdefault(p.cultivation_unit_id, []).append(p)

        o2_latest = _latest_o2_by_pond(db)
        bf_latest = _latest_bf_by_unit(db)

        units_data = []
        totals = {"ok": 0, "alerta": 0, "alarma": 0, "sin_dato": 0}
        for u in units:
            u_ponds = ponds_by_unit.get(u.id, [])
            levels = []
            n_alarm = n_alert = n_nodata = n_stale = 0
            pond_rows = []

            for p in u_ponds:
                r = o2_latest.get(p.id)
                hours, stale = _o2_hours_ago(r, now)
                if r is None:
                    n_nodata += 1
                else:
                    if stale:
                        n_stale += 1
                    if r.alarm_level:
                        levels.append(r.alarm_level)
                        if r.alarm_level == "alarma":
                            n_alarm += 1
                        elif r.alarm_level == "alerta":
                            n_alert += 1
                pond_rows.append({
                    "pond_id": p.id, "pond_name": p.name, "reading": r,
                    "alarm_level": (r.alarm_level if r else None),
                    "consistency_flag": (r.consistency_flag if r else None),
                    "hours_ago": hours, "stale": stale, "has_data": r is not None,
                })

            bf = bf_latest.get(u.id)
            has_bf = bf is not None
            if bf is None:
                n_nodata += 1
            elif bf.alarm_level:
                levels.append(bf.alarm_level)
                if bf.alarm_level == "alarma":
                    n_alarm += 1
                elif bf.alarm_level == "alerta":
                    n_alert += 1

            rollup = wq.worst_level(*levels) if levels else "sin_dato"
            totals[rollup] = totals.get(rollup, 0) + 1
            units_data.append({
                "unit_id": u.id,
                "unit_name": u.name,
                "rollup": rollup,
                "n_ponds": len(u_ponds),
                "has_bf": has_bf,
                "n_alarm": n_alarm,
                "n_alert": n_alert,
                "n_nodata": n_nodata,
                "n_stale": n_stale,
                "pond_rows": pond_rows,
                "bf": bf,
            })

        context = {
            "request": request,
            "msg": msg,
            "units": units_data,
            "totals": totals,
            "open_unit": open,
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

        pond = db.query(Pond).filter(Pond.id == pond_id).first()
        unit_id = pond.cultivation_unit_id if pond else None

        msg = f"Lectura de O2 registrada (estado: {res.alarm_level}"
        if res.consistency_flag == "sospechoso":
            msg += ", terna sospechosa"
        msg += ")."
        url = f"/views/ui/calidad-agua?msg={quote_plus(msg)}"
        if unit_id:
            url += f"&open={unit_id}"
        return RedirectResponse(url=url, status_code=303)
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
        test_specs = load_test_specs(db)
        res = wq.evaluate_biofilter(reading_vals, thresholds=thresholds, test_specs=test_specs)

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
        url = f"/views/ui/calidad-agua?msg={quote_plus(msg)}&open={cultivation_unit_id}"
        return RedirectResponse(url=url, status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Umbrales: vista de configuración (editable)
# ---------------------------------------------------------------------------
# Metadatos de presentación: etiqueta y grupo (biológico vs validación).
THRESHOLD_META = {
    "o2_saturation":      {"label": "Saturación de O₂", "group": "bio"},
    "nh3_n":              {"label": "Amonio no ionizado (NH₃-N)", "group": "bio"},
    "nitrite_n":          {"label": "Nitrito (NO₂-N)", "group": "bio"},
    "ph_delta_tol":       {"label": "ΔpH entrada→salida", "group": "val"},
    "temp_delta_tol":     {"label": "Δtemperatura entrada→salida", "group": "val"},
    "o2_consistency_tol": {"label": "Consistencia terna O₂/temp/saturación", "group": "val"},
}
# n_balance_k se edita en la página de especificaciones de test, no aquí.
THRESHOLD_ORDER = ["o2_saturation", "nh3_n", "nitrite_n",
                   "ph_delta_tol", "temp_delta_tol", "o2_consistency_tol"]
_COMPARATOR_TEXT = {"lt": "dispara si es menor que", "gt": "dispara si es mayor que"}


def _num(v):
    return None if v is None else float(v)


@router.get("/umbrales", response_class=HTMLResponse)
def thresholds_form(request: Request, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        by_param = {r.parameter: r for r in db.query(WaterQualityThreshold).all()}
        bio, val = [], []
        for param in THRESHOLD_ORDER:
            r = by_param.get(param)
            if r is None:
                continue
            meta = THRESHOLD_META.get(param, {"label": param, "group": "val"})
            row = {
                "parameter": param,
                "label": meta["label"],
                "unit": r.unit or "",
                "comparator": r.comparator,
                "comparator_text": _COMPARATOR_TEXT.get(r.comparator, r.comparator),
                "alert_value": _num(r.alert_value),
                "alarm_value": _num(r.alarm_value),
                "active": bool(r.active),
                "is_validation": meta["group"] == "val",
            }
            (val if meta["group"] == "val" else bio).append(row)
        context = {"request": request, "msg": msg, "bio_rows": bio, "val_rows": val,
                   "altitude_m": wq.SITE_ALTITUDE_M}
        html = jinja_env.get_template("calidad_agua_umbrales.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/umbrales")
async def thresholds_save(request: Request):
    form = await request.form()
    db = SessionLocal()
    try:
        rows = {r.parameter: r for r in db.query(WaterQualityThreshold).all()}
        changed = 0
        for param, r in rows.items():
            if param not in THRESHOLD_META:
                continue
            alert = _parse_decimal(form.get(f"alert_{param}"))
            alarm = _parse_decimal(form.get(f"alarm_{param}"))
            active = form.get(f"active_{param}") is not None
            new = (alert, alarm, active)
            old = (_num(r.alert_value), _num(r.alarm_value), bool(r.active))
            if new != old:
                r.alert_value = alert
                r.alarm_value = alarm
                r.active = active
                r.updated_at = datetime.now()
                changed += 1
        db.commit()
        msg = "Umbrales guardados." if changed else "Sin cambios."
        if changed:
            msg = f"{changed} umbral(es) actualizado(s)."
        return RedirectResponse(url=f"/views/ui/calidad-agua/umbrales?msg={quote_plus(msg)}", status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# QR por estanque: código estable + hoja imprimible
# ---------------------------------------------------------------------------
def _generate_qr_code() -> str:
    """Token estable y corto para el QR (E + 8 hex)."""
    return "E" + secrets.token_hex(4).upper()


def ensure_pond_qr_codes(db: Session) -> int:
    """Asigna qr_code a los estanques padre activos que aún no tengan uno.

    Idempotente: solo genera para los que falten. Devuelve cuántos creó.
    """
    pending = (
        db.query(Pond)
        .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None), Pond.qr_code.is_(None))
        .all()
    )
    if not pending:
        return 0
    used = {c for (c,) in db.query(Pond.qr_code).filter(Pond.qr_code.isnot(None)).all()}
    created = 0
    for p in pending:
        code = _generate_qr_code()
        while code in used:
            code = _generate_qr_code()
        p.qr_code = code
        used.add(code)
        created += 1
    db.commit()
    return created


@router.get("/qr", response_class=HTMLResponse)
def qr_labels(request: Request):
    db = SessionLocal()
    try:
        ensure_pond_qr_codes(db)
        unit_names = {u.id: u.name for u in db.query(CultivationUnit).all()}
        ponds = (
            db.query(Pond)
            .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
            .order_by(Pond.name)
            .all()
        )
        # Agrupar por unidad para ordenar la hoja
        groups: dict = {}
        for p in ponds:
            uname = unit_names.get(p.cultivation_unit_id, "Sin unidad")
            groups.setdefault(uname, []).append({
                "pond_name": p.name,
                "unit_name": uname,
                "qr_code": p.qr_code,
                # make_qr fuerza QR estándar (make() elegiría Micro QR para
                # textos cortos, y BarcodeDetector no lee Micro QR).
                "svg": segno.make_qr(p.qr_code, error="m").svg_data_uri(scale=4),
            })
        grouped = [{"unit_name": k, "ponds": v} for k, v in sorted(groups.items())]
        context = {"request": request, "grouped": grouped, "total": len(ponds)}
        html = jinja_env.get_template("calidad_agua_qr.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Especificaciones de tests de N (balance por incertidumbre) — editable
# ---------------------------------------------------------------------------
TEST_ORDER = ["nh4_n", "no2_n", "no3_n"]
TEST_LABELS = {"nh4_n": "Amonio (NH₄-N)", "no2_n": "Nitrito (NO₂-N)", "no3_n": "Nitrato (NO₃-N)"}


def _get_k(db: Session) -> float:
    row = db.query(WaterQualityThreshold).filter_by(parameter="n_balance_k").first()
    if row and row.alarm_value is not None:
        return float(row.alarm_value)
    return 1.0


@router.get("/tests", response_class=HTMLResponse)
def tests_form(request: Request, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        by_test = {s.test: s for s in db.query(WaterQualityTestSpec).all()}
        rows = []
        for t in TEST_ORDER:
            s = by_test.get(t)
            d = wq.DEFAULT_TEST_SPECS[t]
            rows.append({
                "test": t,
                "label": (s.label if s and s.label else TEST_LABELS[t]),
                "resolution": float(s.resolution) if s and s.resolution is not None else d["resolution"],
                "acc_fixed": float(s.acc_fixed) if s and s.acc_fixed is not None else d["acc_fixed"],
                "acc_pct_display": (float(s.acc_pct) if s and s.acc_pct is not None else d["acc_pct"]) * 100.0,
            })
        context = {"request": request, "msg": msg, "rows": rows, "k": _get_k(db)}
        html = jinja_env.get_template("calidad_agua_tests.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/tests")
async def tests_save(request: Request):
    form = await request.form()
    db = SessionLocal()
    try:
        by_test = {s.test: s for s in db.query(WaterQualityTestSpec).all()}
        for t in TEST_ORDER:
            s = by_test.get(t)
            if s is None:
                s = WaterQualityTestSpec(test=t, label=TEST_LABELS[t])
                db.add(s)
            s.resolution = _parse_decimal(form.get(f"resolution_{t}"))
            s.acc_fixed = _parse_decimal(form.get(f"acc_fixed_{t}"))
            pct = _parse_decimal(form.get(f"acc_pct_{t}"))
            s.acc_pct = (pct / 100.0) if pct is not None else None  # % -> fracción
            s.updated_at = datetime.now()

        # Factor k (en la tabla de umbrales)
        k = _parse_decimal(form.get("k"))
        krow = db.query(WaterQualityThreshold).filter_by(parameter="n_balance_k").first()
        if krow is None:
            krow = WaterQualityThreshold(parameter="n_balance_k", comparator="gt",
                                         unit="×U", active=True)
            db.add(krow)
        krow.alarm_value = k if k is not None else 1.0
        krow.updated_at = datetime.now()

        db.commit()
        return RedirectResponse(
            url=f"/views/ui/calidad-agua/tests?msg={quote_plus('Especificaciones guardadas.')}",
            status_code=303)
    finally:
        db.close()
