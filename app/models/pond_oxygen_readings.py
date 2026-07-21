from sqlalchemy import Column, BigInteger, String, Numeric, TIMESTAMP, ForeignKey, Boolean
from app.db.session import Base


class PondOxygenReading(Base):
    """Lectura periódica de oxígeno en un estanque (laguna).

    Se registra cada 2-4 h por estanque padre (no sub-estanque). El operador
    ingresa los tres valores (O2 absoluto, temperatura, saturación); el motor
    de calidad de agua recalcula la saturación teórica a partir de O2 + temp
    (agua dulce, altitud de sitio) y marca `consistency_flag` si la terna no
    cierra dentro de tolerancia. `alarm_level` refleja el umbral de saturación.
    """

    __tablename__ = "pond_oxygen_readings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False, index=True)
    operator_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    reading_datetime = Column(TIMESTAMP, nullable=False)
    # UUID generado por el cliente PWA de terreno (idempotencia de sincronización).
    # NULL para lecturas creadas desde el formulario web.
    client_uuid = Column(String(64), unique=True)

    # Valores ingresados por el operador
    do_mg_l = Column(Numeric(6, 2))            # O2 disuelto absoluto (mg/L)
    water_temp_c = Column(Numeric(5, 2))       # Temperatura efectiva usada (°C): medida o heredada
    # True si water_temp_c se heredó de otro estanque de la misma unidad/ronda
    # (la laguna es homogénea; el operador mide la temp una vez por ronda).
    water_temp_inherited = Column(Boolean, nullable=False, default=False)
    saturation_pct = Column(Numeric(6, 2))     # Saturación ingresada (%)

    # Derivados del motor
    saturation_computed_pct = Column(Numeric(6, 2))  # Saturación teórica (O2+temp)
    consistency_flag = Column(String(20))            # ok | sospechoso
    alarm_level = Column(String(20))                 # ok | alerta | alarma

    observation = Column(String(255))
    created_at = Column(TIMESTAMP)
