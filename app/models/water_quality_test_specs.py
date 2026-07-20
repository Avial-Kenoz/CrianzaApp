from sqlalchemy import Column, BigInteger, String, Numeric, TIMESTAMP
from app.db.session import Base


class WaterQualityTestSpec(Base):
    """Especificaciones de cada test de nitrógeno (reactivo comercial).

    Una fila por test (nh4_n, no2_n, no3_n). El error de cada lectura es
    `acc_fixed + acc_pct·valor` (accuracy del datasheet), en mg/L como N; la
    resolución es solo referencia (no entra al cálculo del balance). Editable
    desde Configuración para que la fórmula del balance de N se ajuste sola.
    """

    __tablename__ = "water_quality_test_specs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    test = Column(String(20), nullable=False, unique=True)  # nh4_n | no2_n | no3_n
    label = Column(String(60))
    resolution = Column(Numeric(10, 5))   # mg/L como N (referencia)
    acc_fixed = Column(Numeric(10, 5))    # término fijo de accuracy (mg/L como N)
    acc_pct = Column(Numeric(6, 4))       # fracción proporcional (0.04 = 4%)
    updated_at = Column(TIMESTAMP)
