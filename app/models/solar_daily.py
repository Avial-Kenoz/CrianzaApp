from sqlalchemy import Column, Date, Numeric, Boolean, String, TIMESTAMP, text
from app.db.session import Base


class SolarDaily(Base):
    """Radiacion solar diaria de Parral, archivo y pronostico.

    Una fila por dia. Mientras el dia es futuro guarda el pronostico; cuando
    pasa se pisa con el archivo (reanalisis). `is_forecast` dice cual esta
    guardado: no son intercambiables, y la correlacion contra la amplitud de
    OD se midio contra el ARCHIVO, asi que el pronostico rinde algo menos.
    """

    __tablename__ = "solar_daily"

    day = Column(Date, primary_key=True)
    ghi_mj_m2 = Column(Numeric(6, 2))
    sunshine_hours = Column(Numeric(5, 2))
    cloud_cover_pct = Column(Numeric(5, 1))
    temp_max_c = Column(Numeric(5, 2))
    temp_min_c = Column(Numeric(5, 2))
    is_forecast = Column(Boolean, nullable=False, default=True)
    source = Column(String(40), nullable=False, default="open-meteo")
    fetched_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
