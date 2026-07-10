"""Vistas de ovulación: modelo del ciclo ovárico del esturión."""
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services import ovulation_cycle

router = APIRouter(prefix="/views", tags=["ovulacion"])

_template_dir = Path(__file__).resolve().parent.parent / "templates"
_jinja = Environment(loader=FileSystemLoader(str(_template_dir)))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/ui/ovulacion/ciclo-ovarico", response_class=HTMLResponse)
def ciclo_ovarico(request: Request, db: Session = Depends(get_db)):
    data = ovulation_cycle.load_or_compute(db)
    html = _jinja.get_template("ciclo_ovarico.html").render(
        {"request": request, "d": data}
    )
    return HTMLResponse(content=html)


@router.post("/ui/ovulacion/ciclo-ovarico/actualizar")
def actualizar_ciclo_ovarico(db: Session = Depends(get_db)):
    ovulation_cycle.compute_and_cache(db)
    # el modelo de crecimiento de ova usa el ρ estacional del de ovulación:
    # se re-estima después, alimentándose de los muestreos nuevos.
    try:
        from app.services import oocyte_growth
        oocyte_growth.estimate_and_cache(db)
    except Exception:
        pass
    return RedirectResponse(url="/views/ui/ovulacion/ciclo-ovarico",
                            status_code=303)
