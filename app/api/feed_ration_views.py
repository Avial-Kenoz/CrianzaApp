"""Ración diaria: bajar el programa de bodega al día a día.

Rutina del encargado de cultivo (en el PC): confirmar lo que se programó para
los días ya pasados (alimentado / no alimentado) y programar los siguientes,
hasta HORIZON_DAYS adelante. Mientras quede un día pasado sin confirmar no se
puede programar: es lo que asegura que al volver de un fin de semana se
confirme lo que se dejó programado.

La bodega (programas, salidas de sacos) no cambia: aquí se registra lo que se
echa al agua. Ver services/feed_ration.py.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader

from app.api.water_quality import feed_limits_by_unit
from app.db.session import SessionLocal
from app.models.cultivation_units import CultivationUnit
from app.models.feed import FeedRation
from app.models.users import User
from app.services import feed_ration as fr

router = APIRouter(prefix="/views", tags=["feed-ration"])
template_dir = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)))

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
SIN_UNIDAD = 0      # carriles sin unidad de cultivo (p. ej. Tranque)


def _day_label(d: date, today: date) -> str:
    rel = {-1: "ayer", 0: "hoy", 1: "mañana"}.get((d - today).days)
    base = "{} {}".format(DIAS[d.weekday()], d.strftime("%d-%m"))
    return "{} · {}".format(rel, base) if rel else base


def _users(db) -> list:
    return [{"id": u.id, "label": " ".join(x for x in [u.name, u.lastname] if x) or u.email}
            for u in db.query(User).filter(User.active.is_(True))
                                   .order_by(User.name, User.lastname).all()]


def _photo_state(lim: dict) -> str:
    if not lim:
        return "sin_dato"
    if lim.get("photo"):
        return "detectada"
    return "no_detectada" if lim.get("photo_evaluable") else "sin_dato"


def _build_context(db, fecha: date, today: date) -> dict:
    carriles = fr.theoretical_by_carril(db)
    programado = fr.programmed_daily_kg(db)
    since = fr.terreno_since(db)
    terreno = fr.terreno_kg(db, since)
    limits = feed_limits_by_unit(db)
    raciones = {r.pond_id: r for r in
                db.query(FeedRation).filter(FeedRation.ration_date == fecha).all()}
    units = {u.id: u for u in db.query(CultivationUnit).all()}

    por_unidad: dict = {}
    for c in carriles.values():
        uid = c["unit_id"] or SIN_UNIDAD
        r = raciones.get(c["pond_id"])
        teo = c["teorica"]
        por_unidad.setdefault(uid, []).append({
            "pond_id": c["pond_id"],
            "name": c["name"],
            "biomass_kg": c["biomass_kg"],
            "weight_g": c["weight_g"],
            "feed_name": c["feed_name"],
            "teorica_kg": teo["kg_day"] if teo else None,
            "teorica_pct": teo["pct"] if teo else None,
            "teorica_avisos": teo["avisos"] if teo else [],
            "programado_kg": programado.get(c["pond_id"]),
            "terreno_kg": terreno.get(c["pond_id"]) if since else None,
            "racion": ({"kg_planned": float(r.kg_planned), "status": r.status,
                        "kg_fed": float(r.kg_fed) if r.kg_fed is not None else None,
                        "split_mode": r.split_mode} if r else None),
        })

    unidades = []
    for uid, cs in por_unidad.items():
        cs.sort(key=lambda c: c["name"])
        u = units.get(uid)
        lim = limits.get(uid) or {}
        cap = lim.get("feed_cap") or {}
        teo = [c["teorica_kg"] for c in cs if c["teorica_kg"] is not None]
        prog = [c["programado_kg"] for c in cs if c["programado_kg"]]
        ter = [c["terreno_kg"] for c in cs if c["terreno_kg"] is not None]
        rac = [c["racion"] for c in cs if c["racion"]]
        modos = {r["split_mode"] for r in rac if r["split_mode"]}
        temp = next((cr["temp_c"] for cr in carriles.values()
                     if (cr["unit_id"] or SIN_UNIDAD) == uid and cr["temp_c"] is not None), None)
        temp_fuente = next((cr["temp_fuente"] for cr in carriles.values()
                            if (cr["unit_id"] or SIN_UNIDAD) == uid and cr["temp_fuente"]), None)
        unidades.append({
            "unit_id": uid,
            "name": u.name if u else "Sin unidad",
            "carriles": cs,
            "biomass_kg": sum(c["biomass_kg"] for c in cs),
            "temp_c": temp,
            "temp_fuente": temp_fuente,
            "teorica_kg": sum(teo) if teo else None,
            "teorica_avisos": sum(1 for c in cs if c["teorica_avisos"]),
            "techo_kg": cap.get("feed_max"),
            "techo_manda": cap.get("manda"),
            "fotosintesis": _photo_state(lim),
            "programado_kg": sum(prog) if prog else None,
            "terreno_kg": sum(ter) if ter else None,
            "racion_kg": sum(r["kg_planned"] for r in rac) if rac else None,
            "racion_status": ({r["status"] for r in rac} if rac else set()),
            "split_mode": (modos.pop() if len(modos) == 1 else None),
            "has_biomass": any(c["biomass_kg"] > 0 for c in cs),
        })
    unidades.sort(key=lambda x: (x["unit_id"] == SIN_UNIDAD, x["name"]))

    pendientes = fr.pending_dates(db, today)
    dias = ([{"fecha": d, "label": _day_label(d, today), "pendiente": True} for d in pendientes]
            + [{"fecha": today + timedelta(days=i), "label": _day_label(today + timedelta(days=i), today),
                "pendiente": False} for i in range(fr.HORIZON_DAYS + 1)])
    programados = {d for (d,) in db.query(FeedRation.ration_date)
                   .filter(FeedRation.ration_date >= today,
                           FeedRation.ration_date <= today + timedelta(days=fr.HORIZON_DAYS))
                   .distinct().all()}
    for d in dias:
        d["programado"] = d["fecha"] in programados or d["pendiente"]
        d["iso"] = d["fecha"].isoformat()

    modo = "confirmar" if fecha < today else "programar"
    return {
        "fecha": fecha, "fecha_iso": fecha.isoformat(),
        "fecha_label": _day_label(fecha, today),
        "today": today, "modo": modo,
        "dias": dias, "pendientes": pendientes,
        "bloqueado": modo == "programar" and bool(pendientes),
        "unidades": unidades,
        "terreno_desde": since,
        "users": _users(db),
        "horizon": fr.HORIZON_DAYS,
    }


@router.get("/ui/feed/racion", response_class=HTMLResponse)
def racion_diaria(request: Request, fecha: Optional[str] = None, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        today = date.today()
        try:
            f = date.fromisoformat(fecha) if fecha else None
        except ValueError:
            f = None
        if f is None:
            pend = fr.pending_dates(db, today)
            f = pend[0] if pend else today + timedelta(days=1)
        if f > today + timedelta(days=fr.HORIZON_DAYS):
            f = today + timedelta(days=fr.HORIZON_DAYS)
        context = _build_context(db, f, today)
        context.update(request=request, msg=msg)
        html = jinja_env.get_template("feed_racion.html").render(context)
        return HTMLResponse(html)
    finally:
        db.close()


def _kg(v) -> Optional[Decimal]:
    try:
        d = Decimal(str(v)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d if d >= 0 else None


def _user_id(db, v) -> Optional[int]:
    try:
        uid = int(v)
    except (TypeError, ValueError):
        return None
    return uid if db.query(User.id).filter(User.id == uid, User.active.is_(True)).first() else None


@router.post("/ui/feed/racion/programar")
async def racion_programar(request: Request):
    """Programa la ración de una unidad para un día: kg por carril (0 = ayuno)."""
    payload = await request.json()
    db = SessionLocal()
    try:
        today = date.today()
        try:
            fecha = date.fromisoformat(payload.get("fecha") or "")
        except ValueError:
            return JSONResponse({"ok": False, "error": "Fecha inválida"}, status_code=400)
        if not (today <= fecha <= today + timedelta(days=fr.HORIZON_DAYS)):
            return JSONResponse({"ok": False, "error": "Solo se programa desde hoy hasta {} días adelante".format(fr.HORIZON_DAYS)}, status_code=400)
        if fr.pending_dates(db, today):
            return JSONResponse({"ok": False, "error": "Primero confirma los días pasados que quedaron programados"}, status_code=409)
        uid = _user_id(db, payload.get("user_id"))
        if uid is None:
            return JSONResponse({"ok": False, "error": "Indica quién programa"}, status_code=400)
        split_mode = payload.get("split_mode") if payload.get("split_mode") in ("biomasa", "iguales") else None

        carriles = fr.carril_inputs(db)
        items = payload.get("items") or []
        if not items:
            return JSONResponse({"ok": False, "error": "Sin carriles"}, status_code=400)
        now = datetime.now()
        for it in items:
            try:
                pid = int(it.get("pond_id"))
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "Carril inválido"}, status_code=400)
            c = carriles.get(pid)
            kg = _kg(it.get("kg"))
            if c is None or kg is None:
                return JSONResponse({"ok": False, "error": "Carril o kg inválido"}, status_code=400)
            r = (db.query(FeedRation)
                 .filter(FeedRation.ration_date == fecha, FeedRation.pond_id == pid).first())
            if r is not None and r.status != "programado":
                return JSONResponse({"ok": False, "error": "{} ya está confirmado ese día".format(c["name"])}, status_code=409)
            if r is None:
                r = FeedRation(ration_date=fecha, pond_id=pid)
                db.add(r)
            r.cultivation_unit_id = c["unit_id"]
            r.feed_type_id = c["feed_type_id"]
            r.kg_planned = kg
            r.split_mode = split_mode
            r.status = "programado"
            r.planned_by = uid
            r.planned_at = now
        db.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        db.rollback()
        return JSONResponse({"ok": False, "error": "Error: {}".format(e)}, status_code=500)
    finally:
        db.close()


@router.post("/ui/feed/racion/confirmar")
async def racion_confirmar(request: Request):
    """Confirma un día ya pasado: por carril, alimentado (con kg reales) o no."""
    payload = await request.json()
    db = SessionLocal()
    try:
        today = date.today()
        try:
            fecha = date.fromisoformat(payload.get("fecha") or "")
        except ValueError:
            return JSONResponse({"ok": False, "error": "Fecha inválida"}, status_code=400)
        if fecha >= today:
            return JSONResponse({"ok": False, "error": "Solo se confirman días ya pasados"}, status_code=400)
        uid = _user_id(db, payload.get("user_id"))
        if uid is None:
            return JSONResponse({"ok": False, "error": "Indica quién confirma"}, status_code=400)
        items = {}
        for it in payload.get("items") or []:
            try:
                items[int(it.get("pond_id"))] = it
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "Carril inválido"}, status_code=400)
        rows = (db.query(FeedRation)
                .filter(FeedRation.ration_date == fecha, FeedRation.status == "programado").all())
        faltan = [r for r in rows if r.pond_id not in items]
        if faltan:
            return JSONResponse({"ok": False, "error": "Faltan carriles por confirmar"}, status_code=400)
        now = datetime.now()
        for r in rows:
            it = items[r.pond_id]
            if it.get("fed"):
                kg = _kg(it.get("kg"))
                if kg is None or kg <= 0:
                    return JSONResponse({"ok": False, "error": "kg inválido en un carril alimentado"}, status_code=400)
                r.status, r.kg_fed = "alimentado", kg
            else:
                r.status, r.kg_fed = "no_alimentado", Decimal("0")
            r.confirmed_by = uid
            r.confirmed_at = now
            note = (it.get("note") or "").strip()
            r.note = note or None
        db.commit()
        return JSONResponse({"ok": True, "pendientes": [d.isoformat() for d in fr.pending_dates(db, today)]})
    except Exception as e:
        db.rollback()
        return JSONResponse({"ok": False, "error": "Error: {}".format(e)}, status_code=500)
    finally:
        db.close()
