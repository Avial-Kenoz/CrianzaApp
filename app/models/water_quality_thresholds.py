from sqlalchemy import Column, BigInteger, String, Numeric, Boolean, TIMESTAMP
from app.db.session import Base


class WaterQualityThreshold(Base):
    """Umbrales y tolerancias configurables del módulo de calidad de agua.

    Una fila por parámetro. `comparator` indica si un valor dispara el nivel
    cuando es menor ('lt') o mayor ('gt') que el umbral. `alert_value` y
    `alarm_value` son nullables (algunos parámetros usan solo uno). Sembrado
    con defaults en la migración; editable desde config/BD sin tocar código.
    """

    __tablename__ = "water_quality_thresholds"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    parameter = Column(String(50), nullable=False, unique=True)
    alert_value = Column(Numeric(10, 4))
    alarm_value = Column(Numeric(10, 4))
    comparator = Column(String(10), nullable=False)  # lt | gt
    unit = Column(String(20))
    description = Column(String(200))
    active = Column(Boolean, nullable=False, default=True)
    updated_at = Column(TIMESTAMP)
