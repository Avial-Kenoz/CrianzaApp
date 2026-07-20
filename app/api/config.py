"""Router de Configuración del sistema (índice de secciones de ajustes).

Página raíz `/views/ui/config` que enlaza a las secciones de configuración
disponibles. Pensada para ir creciendo.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

from app.db.session import SessionLocal
from app.models.users import User

router = APIRouter(prefix="/views/ui/config", tags=["config"])
template_dir = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)))

# Secciones de configuración registradas (título, descripción, URL, ícono emoji)
CONFIG_SECTIONS = [
    {
        "title": "Umbrales de calidad de agua",
        "desc": "Alertas y alarmas de oxígeno, amonio y nitrito, y tolerancias de validación.",
        "url": "/views/ui/calidad-agua/umbrales",
        "icon": "💧",
    },
    {
        "title": "Etiquetas QR de estanques",
        "desc": "Hoja imprimible con el QR de cada estanque para la captura en terreno.",
        "url": "/views/ui/calidad-agua/qr",
        "icon": "🏷️",
    },
    {
        "title": "Especificaciones de tests (balance N)",
        "desc": "Accuracy de amonio/nitrito/nitrato y factor k para validar el balance de nitrógeno.",
        "url": "/views/ui/calidad-agua/tests",
        "icon": "🧪",
    },
    {
        "title": "Gestión de usuarios",
        "desc": "Activa o desactiva usuarios. Los activos aparecen en los selectores de operador.",
        "url": "/views/ui/config/usuarios",
        "icon": "👥",
    },
]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def config_index(request: Request):
    html = jinja_env.get_template("configuracion.html").render(
        {"request": request, "sections": CONFIG_SECTIONS}
    )
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Gestión de usuarios (activar / desactivar)
# ---------------------------------------------------------------------------
@router.get("/usuarios", response_class=HTMLResponse)
def users_page(request: Request, msg: str = None):
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.name, User.lastname).all()
        rows = [{
            "id": u.id,
            "name": " ".join(x for x in [u.name, u.lastname] if x) or "(sin nombre)",
            "email": u.email,
            "rut": u.rut or "",
            "active": bool(u.active),
        } for u in users]
        active_n = sum(1 for r in rows if r["active"])
        context = {"request": request, "msg": msg, "users": rows,
                   "total": len(rows), "active_n": active_n}
        html = jinja_env.get_template("configuracion_usuarios.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/usuarios")
async def users_save(request: Request):
    form = await request.form()
    db = SessionLocal()
    try:
        users = db.query(User).all()
        changed = 0
        for u in users:
            new_active = form.get(f"active_{u.id}") is not None
            if bool(u.active) != new_active:
                u.active = new_active
                u.updated_at = datetime.now()
                changed += 1
        db.commit()
        active_n = sum(1 for u in users if u.active)
        if changed:
            msg = f"{changed} usuario(s) actualizado(s). Activos: {active_n}."
        else:
            msg = "Sin cambios."
        return RedirectResponse(url=f"/views/ui/config/usuarios?msg={quote_plus(msg)}", status_code=303)
    finally:
        db.close()
