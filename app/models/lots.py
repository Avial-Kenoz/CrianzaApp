from sqlalchemy import Column, BigInteger, String, Integer, Boolean, Numeric, TIMESTAMP, ForeignKey
from app.db.session import Base

class Lot(Base):
    __tablename__ = "lots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    species_id = Column(BigInteger, ForeignKey("species.id"), nullable=False)
    name = Column(String(120), nullable=False)
    internal_id = Column(String(120))
    origin = Column(String(120))
    hatch_year = Column(Integer)
    creation_time = Column(TIMESTAMP)
    creation_date = Column(TIMESTAMP)
    national = Column(Boolean, default=False)
    biomass_current = Column(Numeric(14, 3), nullable=True)  # kg — caché actualizado por muestreo y mortalidad/faena
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
