from sqlalchemy import Column, BigInteger, String, Numeric, Boolean, TIMESTAMP
from app.db.session import Base


class SilageThreshold(Base):
    """Parámetros configurables del módulo de molienda y ensilaje.

    Espejo de `water_quality_thresholds`: una fila por parámetro, sembrada con
    defaults en la migración y editable desde Configuración sin tocar código.

    Parámetros sembrados:
      - `ph_limit` (4.0): criterio de aceptación del ensilaje.
      - `drum_capacity_kg`: volumen fijo del tambor, igual para todos.
      - `acid_low_stock_lts`: piso de stock de ácido para alertar.
      - `acid_lts_per_kg_min` / `_max` (0.10 / 0.25): rango de dosis esperado.
        Es solo un chequeo de verosimilitud del número digitado, para cazar
        tipeos; NUNCA invalida una carga. El criterio de aceptación es el pH.
    """

    __tablename__ = "silage_thresholds"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    parameter = Column(String(50), nullable=False, unique=True)
    value = Column(Numeric(12, 4))
    unit = Column(String(20))
    description = Column(String(200))
    active = Column(Boolean, nullable=False, default=True)
    updated_at = Column(TIMESTAMP)
