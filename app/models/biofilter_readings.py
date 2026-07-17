from sqlalchemy import Column, BigInteger, String, Numeric, Date, TIMESTAMP, ForeignKey
from app.db.session import Base


class BiofilterReading(Base):
    """Muestreo diario del biofiltro de una unidad de cultivo (entrada + salida).

    Un biofiltro por unidad de cultivo. En cada punto (entrada/salida) se miden
    pH, temperatura, amonio, nitrito y nitrato. Las especies de nitrógeno se
    guardan expresadas **como N** (NH4-N, NO2-N, NO3-N), de modo que el balance
    de nitrógeno total es la suma directa de las tres.

    Se guardan ambos puntos en la misma fila para poder validar sin joins:
      - conservación de N: |tn_in - tn_out| dentro de tolerancia
      - ΔpH / Δtemp entrada→salida acotados (residencia corta)
      - NH3 no ionizado (nh3_n_*) según pH y temperatura, para alarma de amonio
    """

    __tablename__ = "biofilter_readings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cultivation_unit_id = Column(BigInteger, ForeignKey("cultivation_units.id"), nullable=False, index=True)
    operator_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    reading_date = Column(Date, nullable=False)

    # Entrada
    in_ph = Column(Numeric(4, 2))
    in_temp_c = Column(Numeric(5, 2))
    in_nh4_n = Column(Numeric(8, 3))   # NH4-N (mg/L)
    in_no2_n = Column(Numeric(8, 3))   # NO2-N (mg/L)
    in_no3_n = Column(Numeric(8, 3))   # NO3-N (mg/L)

    # Salida
    out_ph = Column(Numeric(4, 2))
    out_temp_c = Column(Numeric(5, 2))
    out_nh4_n = Column(Numeric(8, 3))
    out_no2_n = Column(Numeric(8, 3))
    out_no3_n = Column(Numeric(8, 3))

    # Derivados del motor
    tn_in = Column(Numeric(9, 3))       # N total entrada (suma de especies como N)
    tn_out = Column(Numeric(9, 3))      # N total salida
    nh3_n_in = Column(Numeric(8, 4))    # NH3-N no ionizado entrada
    nh3_n_out = Column(Numeric(8, 4))   # NH3-N no ionizado salida
    n_balance_flag = Column(String(20))  # ok | sospechoso
    ph_delta_flag = Column(String(20))   # ok | sospechoso
    temp_delta_flag = Column(String(20))  # ok | sospechoso
    alarm_level = Column(String(20))     # ok | alerta | alarma

    observation = Column(String(255))
    created_at = Column(TIMESTAMP)
