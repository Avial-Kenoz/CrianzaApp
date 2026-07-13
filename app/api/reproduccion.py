"""Vistas y API del módulo Reproducción.

F2: vista principal `/views/ui/reproduccion` (Tabla A/B, solo lectura +
expiración lazy). Las acciones (crear selección, fármacos, monitoreo, desove)
llegan en F3/F4.
"""
from pathlib import Path

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services import reproduccion as repro_service
from app.services.reproduccion import ReproduccionError

router = APIRouter(prefix="/views", tags=["reproduccion"])
api_router = APIRouter(prefix="/api/reproduccion", tags=["reproduccion"])

_template_dir = Path(__file__).resolve().parent.parent / "templates"
_jinja = Environment(loader=FileSystemLoader(str(_template_dir)))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _back(next_url: str | None, *, msg: str | None = None, status: str | None = None,
          open_sel: int | None = None) -> RedirectResponse:
    url = next_url or "/views/ui/reproduccion"
    params = []
    if status:
        params.append(f"status={quote_plus(status)}")
    if msg:
        params.append(f"msg={quote_plus(msg)}")
    if open_sel:
        params.append(f"open={open_sel}")
    if params:
        url += ("&" if "?" in url else "?") + "&".join(params)
    return RedirectResponse(url=url, status_code=303)


@router.get("/ui/reproduccion", response_class=HTMLResponse)
def reproduccion_main(
    request: Request,
    status: str | None = None,
    msg: str | None = None,
    open: int | None = None,
    db: Session = Depends(get_db),
):
    ctx = repro_service.build_main_context(db)
    html = _jinja.get_template("reproduccion.html").render(
        {"request": request, "status": status, "msg": msg, "open_sel": open, **ctx}
    )
    return HTMLResponse(content=html)


# ── API de selección (F3) ───────────────────────────────────────────────────
@api_router.post("/selecciones")
def crear_seleccion(
    fish_id: int = Form(...),
    sex: str | None = Form(None),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        repro_service.create_selection(db, fish_id, sex=sex)
    except ReproduccionError as e:
        return _back(next, status="error", msg=str(e))
    return _back(next, status="ok", msg="Pez seleccionado como reproductor.")


@api_router.post("/selecciones/{selection_id}/fail")
def marcar_fallida(
    selection_id: int,
    note: str | None = Form(None),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        repro_service.fail_selection(db, selection_id, note)
    except ReproduccionError as e:
        return _back(next, status="error", msg=str(e))
    return _back(next, status="ok", msg="Selección marcada como fallida.")


@api_router.post("/selecciones/{selection_id}/drugs")
def agregar_farmaco(
    selection_id: int,
    log_date: str = Form(...),
    log_time: str | None = Form(None),
    drug_name: str = Form(...),
    dose: str = Form(...),
    water_temp: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        repro_service.add_drug_log(db, selection_id, log_date=log_date, log_time=log_time,
                                   drug_name=drug_name, dose=dose, water_temp=water_temp)
    except ReproduccionError as e:
        return _back(None, status="error", msg=str(e), open_sel=selection_id)
    return _back(None, status="ok", msg="Fármaco registrado.", open_sel=selection_id)


@api_router.post("/selecciones/{selection_id}/monitoring")
def agregar_monitoreo(
    selection_id: int,
    log_date: str = Form(...),
    sex: str = Form(...),
    sperm_density: str | None = Form(None),
    sperm_motility: str | None = Form(None),
    sperm_time: str | None = Form(None),
    ip_value: str | None = Form(None),
    observation: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        repro_service.add_monitoring_log(db, selection_id, log_date=log_date, sex=sex,
                                         sperm_density=sperm_density, sperm_motility=sperm_motility,
                                         sperm_time=sperm_time, ip_value=ip_value, observation=observation)
    except ReproduccionError as e:
        return _back(None, status="error", msg=str(e), open_sel=selection_id)
    return _back(None, status="ok", msg="Monitoreo registrado.", open_sel=selection_id)


# ── Proceso 2: Desove (F5) ──────────────────────────────────────────────────
def _desove_back(selection_id, *, status=None, msg=None):
    return _back(f"/views/ui/reproduccion/desove/{selection_id}", status=status, msg=msg)


@router.get("/ui/reproduccion/desove/{selection_id}", response_class=HTMLResponse)
def desove_view(selection_id: int, request: Request, status: str | None = None,
                msg: str | None = None, db: Session = Depends(get_db)):
    try:
        ctx = repro_service.build_spawning_context(db, selection_id)
    except ReproduccionError as e:
        return _back("/views/ui/reproduccion", status="error", msg=str(e))
    html = _jinja.get_template("desove.html").render(
        {"request": request, "status": status, "msg": msg, **ctx}
    )
    return HTMLResponse(content=html)


@router.post("/ui/reproduccion/desove/{selection_id}")
async def desove_save(selection_id: int, request: Request, db: Session = Depends(get_db)):
    raw = await request.form()
    data = dict(raw)
    data["male_selection_ids"] = raw.getlist("male_selection_ids")
    data["incubator_id"] = raw.getlist("incubator_id")
    data["incubator_weight"] = raw.getlist("incubator_weight")
    try:
        repro_service.save_spawning(db, selection_id, data)
    except ReproduccionError as e:
        return _desove_back(selection_id, status="error", msg=str(e))
    return _desove_back(selection_id, status="ok", msg="Desove guardado.")


@api_router.post("/desove/{selection_id}/complete")
def desove_complete(selection_id: int, db: Session = Depends(get_db)):
    try:
        batch = repro_service.complete_spawning(db, selection_id)
    except ReproduccionError as e:
        return _desove_back(selection_id, status="error", msg=str(e))
    return _back(f"/views/ui/reproduccion/incubadoras/{batch.id}", status="ok",
                 msg="Desove cerrado. Comienza el monitoreo de incubadoras.")


# ── Proceso 3: Incubadoras (F6) ─────────────────────────────────────────────
def _inc_back(batch_id, *, status=None, msg=None):
    return _back(f"/views/ui/reproduccion/incubadoras/{batch_id}", status=status, msg=msg)


@router.get("/ui/reproduccion/incubadoras/{batch_id}", response_class=HTMLResponse)
def incubadoras_view(batch_id: int, request: Request, status: str | None = None,
                     msg: str | None = None, db: Session = Depends(get_db)):
    try:
        ctx = repro_service.build_incubator_context(db, batch_id)
    except ReproduccionError as e:
        return _back("/views/ui/reproduccion", status="error", msg=str(e))
    html = _jinja.get_template("incubadoras.html").render(
        {"request": request, "status": status, "msg": msg, **ctx}
    )
    return HTMLResponse(content=html)


@api_router.post("/incubadoras/{batch_id}/lot")
def incubadoras_lot(batch_id: int, name: str = Form(...), code: str = Form(...),
                    db: Session = Depends(get_db)):
    try:
        repro_service.save_lot(db, batch_id, name, code)
    except ReproduccionError as e:
        return _inc_back(batch_id, status="error", msg=str(e))
    return _inc_back(batch_id, status="ok", msg="Lote guardado.")


@api_router.post("/incubadoras/{batch_id}/monitoring")
async def incubadoras_monitoring(batch_id: int, request: Request, db: Session = Depends(get_db)):
    data = dict(await request.form())
    try:
        n = repro_service.save_monitoring_round(db, batch_id, data)
    except ReproduccionError as e:
        return _inc_back(batch_id, status="error", msg=str(e))
    return _inc_back(batch_id, status="ok", msg=f"Ronda de monitoreo guardada ({n} incubadora/s).")


@api_router.post("/incubadoras/unidad/{unit_id}/split")
async def incubadoras_split(unit_id: int, request: Request, db: Session = Depends(get_db)):
    raw = await request.form()
    targets = list(zip(raw.getlist("base_id"), raw.getlist("weight")))
    notes = raw.get("notes")
    from app.models.incubator_units import IncubatorUnit as _IU
    u = db.get(_IU, unit_id)
    batch_id = u.incubator_batch_id if u else None
    try:
        repro_service.split_unit(db, unit_id, targets, notes)
    except ReproduccionError as e:
        return _inc_back(batch_id, status="error", msg=str(e))
    return _inc_back(batch_id, status="ok", msg="Incubadora particionada.")


@api_router.post("/incubadoras/unidad/{unit_id}/hatch")
def incubadoras_hatch(unit_id: int, target_pond_id: int = Form(...), db: Session = Depends(get_db)):
    from app.models.incubator_units import IncubatorUnit as _IU
    u = db.get(_IU, unit_id)
    batch_id = u.incubator_batch_id if u else None
    try:
        repro_service.hatch_unit(db, unit_id, target_pond_id)
    except ReproduccionError as e:
        return _inc_back(batch_id, status="error", msg=str(e))
    return _inc_back(batch_id, status="ok", msg="Incubadora eclosionada.")


@api_router.post("/incubadoras/{batch_id}/complete")
def incubadoras_complete(batch_id: int, db: Session = Depends(get_db)):
    try:
        repro_service.complete_batch(db, batch_id)
    except ReproduccionError as e:
        return _inc_back(batch_id, status="error", msg=str(e))
    return _inc_back(batch_id, status="ok", msg="Proceso 3 cerrado.")
