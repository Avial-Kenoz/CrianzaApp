"""Job agendado que refresca el cache operativo por estanque (n_fish_cached, etc.).

Motivo: la vista de lista `/ui/ponds` lee `pond.n_fish_cached` (una foto persistida),
mientras que el detalle `/ui/ponds/{id}` recalcula en vivo. Si el cache no se refresca
tras un movimiento, ambas vistas divergen. Este job lo mantiene fresco periódicamente.

Corre dentro del proceso de uvicorn (single worker vía watchdog → una sola instancia).
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db.session import SessionLocal
from app.api.views import rebuild_all_pond_runtime_cache

logger = logging.getLogger("pond_cache_scheduler")

_scheduler: BackgroundScheduler | None = None


def _run_rebuild() -> None:
    db = SessionLocal()
    try:
        updated = rebuild_all_pond_runtime_cache(db)
        logger.info("pond runtime cache refrescado: %s estanques", updated)
    except Exception:
        db.rollback()
        logger.exception("fallo al refrescar pond runtime cache")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    """Arranca el scheduler: Lun-Vie, cada 2h de 08:00 a 18:00."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="America/Santiago")
    scheduler.add_job(
        _run_rebuild,
        trigger=CronTrigger(day_of_week="mon-fri", hour="8-18/2", minute=0),
        id="pond_runtime_cache_rebuild",
        name="Refrescar cache de estanques (Lun-Vie 8-18/2h)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("pond_cache_scheduler iniciado (Lun-Vie 8-18 cada 2h)")
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
