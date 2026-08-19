from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pathlib import Path

from app.api.fish import router as fish_router
from app.api.ponds import router as ponds_router
from app.api.movements import router as movements_router
from app.api.fish_samplings import router as fish_samplings_router
from app.api.mortality_reports import router as mortality_reports_router
from app.api.depuration_samplings import router as depuration_samplings_router
from app.api.catalogs import router as catalogs_router
from app.api.views import router as views_router
from app.api.cultivation_declarations import router as cultivation_declarations_router
from app.api.feed_endpoints import router as feed_router
from app.api.ovulation import router as ovulation_router
from app.api.reproduccion import router as reproduccion_router, api_router as reproduccion_api_router
from app.api.water_quality import router as water_quality_router
from app.api.config import router as config_router
from app.api.field import router as field_router
from app.api.sexado import router as sexado_router, admin_router as sexado_admin_router
from app.services.pond_cache_scheduler import start_scheduler, shutdown_scheduler

app = FastAPI(title="FastApp Etapa 1", version="0.1.0")


@app.on_event("startup")
def _start_pond_cache_scheduler() -> None:
    start_scheduler()


@app.on_event("shutdown")
def _stop_pond_cache_scheduler() -> None:
    shutdown_scheduler()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class NoCacheStatic(StaticFiles):
    """StaticFiles que obliga a revalidar el shell de la PWA en cada carga.

    Sin `Cache-Control`, el navegador aplica caché heurístico y el service
    worker se queda con la copia vieja: el 19/08/2026 un tablet siguió corriendo
    código de días atrás pese a reinstalar la app, sin ninguna señal visible.
    `no-cache` no significa "no guardar": guarda igual, pero revalida contra el
    servidor, así que un 304 sigue siendo barato en LAN y el modo offline no se
    ve afectado.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
# PWA de captura en terreno (SPA offline, mismo origen). Sirve app/field_client
# en /captura; el service worker en /captura/sw.js controla ese ámbito.
app.mount("/captura", NoCacheStatic(directory=str(Path(__file__).parent / "field_client"), html=True), name="captura")
# PWA de sexado offline (clasificación/movimientos por estanque)
app.mount("/sexado_offline", NoCacheStatic(directory=str(Path(__file__).parent / "sexado_client"), html=True), name="sexado_offline")


app.include_router(fish_router)
app.include_router(ponds_router)
app.include_router(movements_router)
app.include_router(fish_samplings_router)
app.include_router(mortality_reports_router)
app.include_router(depuration_samplings_router)
app.include_router(catalogs_router)
app.include_router(feed_router)
app.include_router(views_router)
app.include_router(cultivation_declarations_router)
app.include_router(ovulation_router)
app.include_router(reproduccion_router)
app.include_router(reproduccion_api_router)
app.include_router(water_quality_router)
app.include_router(config_router)
app.include_router(field_router)
app.include_router(sexado_router)
app.include_router(sexado_admin_router)

@app.get("/")
def root():
    return RedirectResponse(url="/views/ui/ponds")
