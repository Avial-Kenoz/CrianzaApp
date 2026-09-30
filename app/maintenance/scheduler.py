"""Job del módulo de mantenimiento: resumen de fallas menores y repetición de
alarmas P1 sin movimiento (PR4). Cada 5 minutos, 24/7.

Separado de los jobs de calidad de agua (`wq_alerts_scheduler`) aunque viva en
el mismo proceso: si mantenimiento falla, no puede arrastrar a las alertas de
oxígeno, ni al revés.

Solo arranca donde los envíos están habilitados (`MNT_BOT_ENABLED=1`, es decir,
producción): un servidor de desarrollo apunta a la misma base y no debe
escribirle a nadie (ver `notify.envios_habilitados`).
"""
from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db.session import SessionLocal
from app.maintenance import notify

logger = logging.getLogger("mnt_scheduler")
_scheduler: Optional[BackgroundScheduler] = None


def _tick() -> None:
    # Cada envío en su propio try: que falle el resumen no impide repetir un P1.
    for nombre, fn in (("resumen", notify.enviar_resumenes), ("repeticion_p1", notify.repetir_p1)):
        db = SessionLocal()
        try:
            n = fn(db)
            if n:
                logger.info("mantenimiento: %s → %s mensaje(s)", nombre, n)
        except Exception:
            db.rollback()
            logger.exception("mantenimiento: falló %s", nombre)
        finally:
            db.close()


def iniciar() -> None:
    global _scheduler
    if not notify.envios_habilitados():
        logger.info("mnt scheduler: deshabilitado (MNT_BOT_ENABLED != 1)")
        return
    if _scheduler is not None:
        return
    s = BackgroundScheduler(timezone="America/Santiago")
    s.add_job(_tick, trigger=CronTrigger(minute="*/5"), id="mnt_resumen_p1",
              name="Mantenimiento: resumen y repetición P1 (cada 5 min)",
              max_instances=1, coalesce=True, misfire_grace_time=300)
    s.start()
    _scheduler = s
    logger.info("mnt scheduler iniciado (cada 5 min)")


def detener() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
