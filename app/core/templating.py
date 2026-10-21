"""Entorno Jinja único de la app.

Antes cada router creaba el suyo (13 en total) y solo dos escapaban el HTML:
una nota de pez o un aviso de mantenimiento con `<` se inyectaba tal cual en la
página. Ahora todos renderizan con este, que escapa por defecto. Lo que de verdad
es HTML armado en Python se marca con `Markup(...)` en el handler o `|safe` en la
plantilla; para pasar datos a un <script> usar `|tojson`, nunca un json.dumps
interpolado.

Filtros o globales comunes se registran aquí, una sola vez.
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

# Para los handlers que usan TemplateResponse: mismo entorno, mismas reglas.
templates = Jinja2Templates(env=jinja_env)
