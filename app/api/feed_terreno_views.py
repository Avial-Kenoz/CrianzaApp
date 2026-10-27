"""Inventario en terreno (tarro de cada carril): saldo, alertas y conteos.

Vista nueva y separada: la ración diaria y la bodega siguen como están hasta
que esto reemplace al programa semanal. Ver services/feed_terreno.py.
"""
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.templating import jinja_env
from app.db.session import SessionLocal
from app.models.feed import FeedType
from app.models.feed_terreno import FeedTarro, FeedTerrenoCount, FeedTerrenoMove
from app.models.ponds import Pond
from app.models.users import User
from app.services import feed_terreno as ft

router = APIRouter(prefix="/views", tags=["feed-terreno"])


def _users(db) -> list:
    return [{"id": u.id, "label": " ".join(x for x in [u.name, u.lastname] if x) or u.email}
            for u in db.query(User).filter(User.active.is_(True))
                                   .order_by(User.name, User.lastname).all()]


def _who(db, user_id):
    try:
        u = db.get(User, int(user_id))
    except (TypeError, ValueError):
        u = None
    if u is None or not u.active:
        return None, None
    return u.id, " ".join(x for x in [u.name, u.lastname] if x) or u.email


def _fecha(v, default: date) -> date:
    try:
        return date.fromisoformat(v) if v else default
    except ValueError:
        return default


def _carriles(db) -> list:
    """Carriles (estanques padre activos) para elegir destino de un traspaso."""
    return [{"id": p.id, "name": p.name} for p in
            db.query(Pond).filter(Pond.parent_pond_id.is_(None), Pond.state != "inactive")
                          .order_by(Pond.cultivation_unit_id, Pond.name).all()]


@router.get("/ui/feed/terreno", response_class=HTMLResponse)
def terreno_view(request: Request):
    db = SessionLocal()
    try:
        today = date.today()
        st = ft.estado(db, today)
        ctx = dict(st, request=request, today=today, users=_users(db),
                   motivos=ft.motivos_para(False), carriles_destino=_carriles(db),
                   tope_default=ft.DEFAULT_TOPE_KG, cobertura_max=ft.COBERTURA_MAX_DIAS)
        return HTMLResponse(jinja_env.get_template("feed_terreno.html").render(ctx))
    finally:
        db.close()


@router.get("/ui/feed/terreno/avisos")
def terreno_avisos():
    """Cantidad de avisos de los tarros (contador de la pestaña)."""
    db = SessionLocal()
    try:
        return JSONResponse({"n": len(ft.estado(db, date.today())["alertas"])})
    finally:
        db.close()


@router.get("/ui/feed/terreno/carril/{pond_id}", response_class=HTMLResponse)
def terreno_carril(request: Request, pond_id: int):
    db = SessionLocal()
    try:
        pond = db.get(Pond, pond_id)
        if pond is None:
            return HTMLResponse("Carril no encontrado", status_code=404)
        conteos = (db.query(FeedTerrenoCount).filter(FeedTerrenoCount.pond_id == pond_id)
                   .order_by(FeedTerrenoCount.count_date.desc(), FeedTerrenoCount.id.desc()).all())
        feeds = {f.id: f.name for f in db.query(FeedType).all()}
        users = {u["id"]: u["label"] for u in _users(db)}
        moves = {}
        for m in db.query(FeedTerrenoMove).filter(FeedTerrenoMove.count_id.in_([c.id for c in conteos] or [0])).all():
            moves.setdefault(m.count_id, []).append(m)
        ctx = {"request": request, "pond": pond, "hist": ft.carril_historial(db, pond_id),
               "since": ft.start_date(db), "conteos": conteos, "moves": moves, "feeds": feeds,
               "users": users, "motivo_label": ft.MOTIVO_LABEL}
        return HTMLResponse(jinja_env.get_template("feed_terreno_carril.html").render(ctx))
    finally:
        db.close()


@router.post("/ui/feed/terreno/papel")
async def terreno_papel(request: Request):
    """Saldo según el sistema para un carril/alimento a una fecha y momento."""
    p = await request.json()
    db = SessionLocal()
    try:
        fecha = _fecha(p.get("fecha"), date.today())
        kg = ft.paper_kg(db, int(p.get("pond_id")), p.get("feed_type_id") or None,
                         fecha, p.get("momento") or "despues")
        return JSONResponse({"ok": True, "paper_kg": kg})
    finally:
        db.close()


@router.post("/ui/feed/terreno/conteo")
async def terreno_conteo(request: Request):
    p = await request.json()
    db = SessionLocal()
    try:
        uid, who = _who(db, p.get("user_id"))
        if uid is None:
            return JSONResponse({"ok": False, "error": "Elige quién registra"}, status_code=422)
        try:
            cnt = ft.registrar_conteo(
                db, pond_id=int(p.get("pond_id")), feed_type_id=p.get("feed_type_id") or None,
                fecha=_fecha(p.get("fecha"), date.today()), momento=p.get("momento") or "despues",
                counted_kg=float(p.get("counted_kg")), lineas=p.get("lineas") or [],
                user_id=uid, who=who, note=p.get("note"))
        except (ft.ConteoError, TypeError, ValueError) as e:
            db.rollback()
            return JSONResponse({"ok": False, "error": str(e) or "Datos inválidos"}, status_code=422)
        db.commit()
        return JSONResponse({"ok": True, "count_id": cnt.id})
    finally:
        db.close()


@router.post("/ui/feed/terreno/conteo/{count_id}/anular")
async def terreno_anular(request: Request, count_id: int):
    p = await request.json()
    db = SessionLocal()
    try:
        uid, _ = _who(db, p.get("user_id"))
        if uid is None:
            return JSONResponse({"ok": False, "error": "Elige quién registra"}, status_code=422)
        try:
            ft.anular_conteo(db, count_id, uid)
        except ft.ConteoError as e:
            db.rollback()
            return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@router.post("/ui/feed/terreno/tope")
async def terreno_tope(request: Request):
    p = await request.json()
    db = SessionLocal()
    try:
        uid, _ = _who(db, p.get("user_id"))
        if uid is None:
            return JSONResponse({"ok": False, "error": "Elige quién registra"}, status_code=422)
        try:
            pond_id, kg = int(p.get("pond_id")), float(p.get("capacity_kg"))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "Tope inválido"}, status_code=422)
        if kg <= 0 or db.get(Pond, pond_id) is None:
            return JSONResponse({"ok": False, "error": "Tope inválido"}, status_code=422)
        from datetime import datetime
        t = db.get(FeedTarro, pond_id) or FeedTarro(pond_id=pond_id)
        t.capacity_kg, t.updated_by, t.updated_at = kg, uid, datetime.utcnow()
        db.add(t)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Conteo inicial (día 0): todos los tarros y la bodega principal el mismo día
# ---------------------------------------------------------------------------
@router.get("/ui/feed/terreno/inicial", response_class=HTMLResponse)
def terreno_inicial(request: Request):
    db = SessionLocal()
    try:
        today = date.today()
        fecha = _fecha(request.query_params.get("fecha"), today)
        momento = request.query_params.get("momento") or "antes"
        st = ft.estado(db, today)
        evs = ft.events(db)
        filas = []
        for u in st["unidades"]:
            for c in u["carriles"]:
                sim = ft.simular(evs.get(c["pond_id"], []), fecha, momento)["saldos"]
                for a in c["alimentos"]:
                    filas.append({"unit": u["name"], "pond_id": c["pond_id"], "name": c["name"],
                                  "feed_type_id": a["feed_type_id"], "feed_name": a["feed_name"],
                                  "bag_kg": a["bag_kg"],
                                  "paper_kg": round(sim.get(a["feed_type_id"], 0.0), 2)})
        bodega = [{"feed_type_id": f.id, "name": f.name, "bag_kg": float(f.bag_kg or 0),
                   "papel": ft.bodega_bags(db, f.id)}
                  for f in db.query(FeedType).filter(FeedType.active == 1).order_by(FeedType.name).all()]
        ctx = {"request": request, "today": today, "fecha": fecha, "momento": momento,
               "since": st["since"], "hecho": st["conteo_inicial"], "filas": filas, "bodega": bodega,
               "users": _users(db), "motivos": ft.motivos_para(True)}
        return HTMLResponse(jinja_env.get_template("feed_terreno_inicial.html").render(ctx))
    finally:
        db.close()


@router.post("/ui/feed/terreno/inicial")
async def terreno_inicial_save(request: Request):
    p = await request.json()
    db = SessionLocal()
    try:
        uid, who = _who(db, p.get("user_id"))
        if uid is None:
            return JSONResponse({"ok": False, "error": "Elige quién registra"}, status_code=422)
        if db.query(FeedTerrenoCount.id).filter(FeedTerrenoCount.is_initial.is_(True),
                                                 FeedTerrenoCount.annulled_at.is_(None)).first():
            return JSONResponse({"ok": False, "error": "El conteo inicial ya se hizo"}, status_code=409)
        fecha = _fecha(p.get("fecha"), date.today())
        momento = p.get("momento") or "antes"
        n = 0
        for f in p.get("filas") or []:
            if f.get("counted_kg") in (None, ""):
                continue
            try:
                ft.registrar_conteo(
                    db, pond_id=int(f["pond_id"]), feed_type_id=f.get("feed_type_id") or None,
                    fecha=fecha, momento=momento, counted_kg=float(f["counted_kg"]),
                    lineas=f.get("lineas") or [], user_id=uid, who=who, is_initial=True,
                    note="Conteo inicial")
                n += 1
            except (ft.ConteoError, TypeError, ValueError, KeyError) as e:
                db.rollback()
                return JSONResponse({"ok": False, "error": "{} · {}: {}".format(
                    f.get("name") or f.get("pond_id"), f.get("feed_name") or "", e)}, status_code=422)
        if not n:
            db.rollback()
            return JSONResponse({"ok": False, "error": "No hay ningún tarro contado"}, status_code=422)
        ajustes = 0
        for b in p.get("bodega") or []:
            if b.get("real_bags") in (None, ""):
                continue
            try:
                real = int(b["real_bags"])
            except (TypeError, ValueError):
                db.rollback()
                return JSONResponse({"ok": False, "error": "Sacos de bodega inválidos"}, status_code=422)
            if ft.ajustar_bodega(db, int(b["feed_type_id"]), real,
                                 "Conteo inicial terreno {}: diferencia no explicada por los tarros"
                                 .format(fecha.strftime("%d-%m-%Y")), who) is not None:
                ajustes += 1
        db.commit()
        return JSONResponse({"ok": True, "conteos": n, "ajustes_bodega": ajustes})
    finally:
        db.close()
