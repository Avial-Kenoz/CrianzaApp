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

app = FastAPI(title="FastApp Etapa 1", version="0.1.0")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


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

@app.get("/")
def root():
    return RedirectResponse(url="/views/ui/ponds")
