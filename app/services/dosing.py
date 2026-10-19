"""Instrucciones de dosificación de biofiltros: lo que el supervisor manda a la
tablet (/dosificacion) y lo que se lleva aplicado contra ella.

Compartido por la vista de la unidad (escritorio, donde se instruye) y la API
de la PWA (/api/dosing/v1, donde se lee y se registra). La regla central vive
aquí para que las dos puntas no diverjan: **la tablet ve la cantidad instruida,
nunca la sugerencia del modelo**. La sugerencia se guarda junto a la
instrucción solo para auditoría.
"""

from datetime import datetime, date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.dosing_instructions import DosingInstruction
from app.models.water_treatment_doses import WaterTreatmentDose

# (valor guardado, etiqueta). Los valores son los de water_treatment_doses:
# "bicarbonato" se mantiene así porque las cargas ya registradas lo usan.
PRODUCTS = [
    ("carbonato de calcio", "Caliza (carbonato de calcio)"),
    ("bicarbonato", "Bicarbonato de sodio"),
    ("sal", "Sal"),
]
PRODUCT_LABELS = dict(PRODUCTS)


def active_instructions(db: Session, unit_id: Optional[int] = None) -> list:
    q = db.query(DosingInstruction).filter(DosingInstruction.ended_at.is_(None))
    if unit_id is not None:
        q = q.filter(DosingInstruction.cultivation_unit_id == unit_id)
    return q.order_by(DosingInstruction.cultivation_unit_id,
                      DosingInstruction.product).all()


def issue(db: Session, unit_id: int, product: str, kg_day: float,
          suggested_kg_day: Optional[float] = None, note: Optional[str] = None,
          issued_by_id: Optional[int] = None) -> DosingInstruction:
    """Reemplaza la instrucción vigente de (unidad, producto) por una nueva.

    Se cierra la anterior y se hace flush ANTES de insertar: el índice único
    parcial no admite dos vigentes ni por un instante dentro de la transacción.
    """
    if product not in PRODUCT_LABELS:
        raise ValueError(f"Producto desconocido: {product}")
    if kg_day is None or kg_day < 0:
        raise ValueError("La cantidad debe ser 0 o más")
    now = datetime.now()
    end(db, unit_id, product, now)
    db.flush()
    ins = DosingInstruction(cultivation_unit_id=unit_id, product=product,
                            kg_day=kg_day, suggested_kg_day=suggested_kg_day,
                            note=(note or None), issued_by_id=issued_by_id,
                            issued_at=now)
    db.add(ins)
    return ins


def end(db: Session, unit_id: int, product: str,
        at: Optional[datetime] = None) -> int:
    """Cierra la instrucción vigente (la tablet deja de mostrar ese producto)."""
    return (db.query(DosingInstruction)
            .filter(DosingInstruction.cultivation_unit_id == unit_id,
                    DosingInstruction.product == product,
                    DosingInstruction.ended_at.is_(None))
            .update({DosingInstruction.ended_at: at or datetime.now()},
                    synchronize_session=False))


def applied_on(db: Session, day: date, unit_ids=None) -> dict:
    """kg aplicados en el día por (unidad, producto), vengan de la tablet o
    del escritorio: lo que importa contra la instrucción es lo que entró al
    agua, no por dónde se anotó."""
    start = datetime.combine(day, datetime.min.time())
    q = (db.query(WaterTreatmentDose.cultivation_unit_id,
                  WaterTreatmentDose.product,
                  func.sum(WaterTreatmentDose.kg))
         .filter(WaterTreatmentDose.applied_at >= start,
                 WaterTreatmentDose.applied_at < start + timedelta(days=1)))
    if unit_ids is not None:
        q = q.filter(WaterTreatmentDose.cultivation_unit_id.in_(list(unit_ids)))
    out = {}
    for uid, product, kg in q.group_by(WaterTreatmentDose.cultivation_unit_id,
                                       WaterTreatmentDose.product).all():
        out[(uid, product)] = float(kg or 0)
    return out
