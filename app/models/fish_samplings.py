from sqlalchemy import Column, BigInteger, String, Integer, Numeric, TIMESTAMP, ForeignKey
from app.db.session import Base

class FishSampling(Base):
    __tablename__ = "fish_samplings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=False)
    length = Column(Numeric(10,3))
    weight = Column(Numeric(10,3))
    oocyte_size = Column(Numeric(10,3))
    color = Column(String(80))
    gonad_size = Column(Numeric(10,3))
    flavor = Column(String(80))
    development_state = Column(String(80))
    harvest_date = Column(TIMESTAMP)
    registry_time = Column(TIMESTAMP)
    diameter = Column(Numeric(10,3))
    agglomeration = Column(Integer)
    turgor = Column(Integer)
    fat = Column(Integer)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
