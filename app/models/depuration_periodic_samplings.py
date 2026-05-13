from sqlalchemy import Column, BigInteger, String, Numeric, TIMESTAMP, Date, ForeignKey
from app.db.session import Base

class DepurationPeriodicSampling(Base):
    __tablename__ = "depuration_periodic_samplings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    fish_id = Column(BigInteger, ForeignKey("fish.id"))
    pond_id = Column(BigInteger, ForeignKey("ponds.id"))
    development_state = Column(String(80))
    width = Column(Numeric(10,3))
    heigh = Column(Numeric(10,3))
    weight = Column(Numeric(10,3))
    harvest_date = Column(Date)
    color = Column(String(80))
    gonad_dimension = Column(Numeric(10,3))
    oocyte_size = Column(Numeric(10,3))
    flavor = Column(String(80))
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
