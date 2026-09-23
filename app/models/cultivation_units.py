from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Numeric, Boolean
from app.db.session import Base

class CultivationUnit(Base):
    __tablename__ = "cultivation_units"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    name = Column(String(120), nullable=False)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)

    # Config para los indicadores de salud del biofiltro (migracion 20260907_01).
    # NO hay caudal a proposito: k se calcula desde el alimento y Q se cancela.
    media_volume_m3 = Column(Numeric(10, 2))          # volumen de medio filtrante
    is_recirculating = Column(Boolean, nullable=False, default=True)
    # Agua fresca que entra (= purga que sale). Segundo sumidero de N: se lleva
    # amonio a la concentracion de la laguna, y hay que descontarlo antes de
    # atribuirle al biofiltro todo el nitrogeno del alimento.
    freshwater_l_s = Column(Numeric(8, 2))
