"""Salidas de bodega por pedido de reposición de tarros (pestaña Salidas).

Reemplaza al programa semanal: se arma el pedido con los sacos sugeridos por
carril, se emite (documento de repartición) y se marca "salió" o se anula.
Ver services/feed_pedidos.py.
"""
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.templating import jinja_env
from app.db.session import SessionLocal
from app.models.users import User
from app.services import feed_pedidos as fp

router = APIRouter(prefix="/views", tags=["feed-pedidos"])


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


def _int(v, default, lo, hi):
    try:
        return min(hi, max(lo, int(v)))
    except (TypeError, ValueError):
        return default


@router.get("/ui/feed/pedidos", response_class=HTMLResponse)
def pedidos_lista(request: Request):
    db = SessionLocal()
    try:
        users = _users(db)
        ctx = {"request": request, "pedidos": fp.lista(db), "users": users,
               "user_label": {u["id"]: u["label"] for u in users}, "stock": fp.stock(db),
               "msg": request.query_params.get("msg")}
        return HTMLResponse(jinja_env.get_template("feed_pedidos.html").render(ctx))
    finally:
        db.close()


@router.get("/ui/feed/pedidos/nuevo", response_class=HTMLResponse)
def pedido_nuevo(request: Request):
    db = SessionLocal()
    try:
        qp = request.query_params
        n = _int(qp.get("n"), fp.DEFAULT_N, 1, fp.MAX_N)
        mn = _int(qp.get("min"), fp.DEFAULT_MIN, 0, n)
        sem = _int(qp.get("sem"), fp.DEFAULT_DIAS_SEMANA, 1, 7)
        today = date.today()
        ctx = {"request": request, "today": today, "n_dias": n, "min": mn, "sem": sem, "max_n": fp.MAX_N,
               "unidades": fp.sugerencia(db, today, n, mn, sem), "stock": fp.stock(db),
               "users": _users(db)}
        return HTMLResponse(jinja_env.get_template("feed_pedido_nuevo.html").render(ctx))
    finally:
        db.close()


@router.post("/ui/feed/pedidos")
async def pedido_emitir(request: Request):
    p = await request.json()
    db = SessionLocal()
    try:
        uid, _ = _who(db, p.get("user_id"))
        if uid is None:
            return JSONResponse({"ok": False, "error": "Elige quién registra"}, status_code=422)
        try:
            ped = fp.emitir(db, lineas=p.get("lineas") or [], n_dias=int(p.get("n")),
                            min_dias=int(p.get("min")), dias_semana=int(p.get("sem")),
                            user_id=uid, note=p.get("note"))
        except (fp.PedidoError, TypeError, ValueError) as e:
            db.rollback()
            return JSONResponse({"ok": False, "error": str(e) or "Datos inválidos"}, status_code=422)
        db.commit()
        return JSONResponse({"ok": True, "id": ped.id, "code": ped.code})
    finally:
        db.close()


@router.get("/ui/feed/pedidos/{pedido_id}", response_class=HTMLResponse)
def pedido_detalle(request: Request, pedido_id: int):
    db = SessionLocal()
    try:
        d = fp.detalle(db, pedido_id)
        if d is None:
            return HTMLResponse("Pedido no encontrado", status_code=404)
        users = _users(db)
        ctx = dict(d, request=request, today=date.today(), users=users,
                   user_label={u["id"]: u["label"] for u in users},
                   imprimir=request.query_params.get("imprimir") == "1")
        return HTMLResponse(jinja_env.get_template("feed_pedido.html").render(ctx))
    finally:
        db.close()


@router.post("/ui/feed/pedidos/{pedido_id}/salio")
async def pedido_salio(request: Request, pedido_id: int):
    p = await request.json()
    db = SessionLocal()
    try:
        uid, who = _who(db, p.get("user_id"))
        if uid is None:
            return JSONResponse({"ok": False, "error": "Elige quién registra"}, status_code=422)
        try:
            fecha = date.fromisoformat(p.get("fecha") or date.today().isoformat())
            fp.marcar_salio(db, pedido_id, fecha, uid, who)
        except (fp.PedidoError, ValueError) as e:
            db.rollback()
            return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@router.post("/ui/feed/pedidos/{pedido_id}/anular")
async def pedido_anular(request: Request, pedido_id: int):
    p = await request.json()
    db = SessionLocal()
    try:
        uid, _ = _who(db, p.get("user_id"))
        if uid is None:
            return JSONResponse({"ok": False, "error": "Elige quién registra"}, status_code=422)
        try:
            fp.anular(db, pedido_id, uid)
        except fp.PedidoError as e:
            db.rollback()
            return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()
