"""Job que corre el motor de alertas de calidad de agua cada pocos minutos.

Separado de `pond_cache_scheduler` a propósito, aunque ambos vivan en el mismo
proceso: aquél refresca un caché en horario de oficina (Lun-Vie 8-18) y éste
tiene que correr **de noche y en fin de semana**, que es justamente cuando no
hay nadie mirando el panel.

Por qué hace falta si el ingreso ya dispara la detección: los dos eventos que
más importan son **ausencias** —la ronda que no se tomó, la alarma que nadie
reconoció— y una ausencia no genera ningún POST que la delate. Sin este job,
la única forma de enterarse de que nadie midió sería que alguien midiera.

Cada 5 minutos, 24/7. El costo es una pasada del motor: un puñado de consultas
sobre la última lectura por estanque.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db.session import SessionLocal
from app.services.wq_alerts import run_detection

logger = logging.getLogger("wq_alerts_scheduler")

_scheduler: BackgroundScheduler | None = None


def _run_detection() -> None:
    db = SessionLocal()
    try:
        res = run_detection(db)
        # Se registra sólo cuando algo cambió: un log cada 5 minutos diciendo
        # "nada" esconde el que sí importa.
        if res["abiertas"] or res["escaladas"] or res["cerradas"] or res["reconocidas"]:
            logger.info("alertas calidad de agua: %s", res)
    except Exception:
        db.rollback()
        logger.exception("fallo la pasada del motor de alertas")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="America/Santiago")
    scheduler.add_job(
        _run_detection,
        trigger=CronTrigger(minute="*/5"),
        id="wq_alerts_detection",
        name="Detectar alertas de calidad de agua (cada 5 min)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("wq_alerts_scheduler iniciado (cada 5 min)")
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
