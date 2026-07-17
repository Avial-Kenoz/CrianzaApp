"""Router de Configuración del sistema (índice de secciones de ajustes).

Página raíz `/views/ui/config` que enlaza a las secciones de configuración
disponibles. Hoy: umbrales de calidad de agua. Pensada para ir creciendo.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

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
]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def config_index(request: Request):
    html = jinja_env.get_template("configuracion.html").render(
        {"request": request, "sections": CONFIG_SECTIONS}
    )
    return HTMLResponse(content=html)
