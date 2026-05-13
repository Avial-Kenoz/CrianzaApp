from sqlalchemy import Column, BigInteger, String, Integer, TIMESTAMP, ForeignKey
from app.db.session import Base

class PondMovement(Base):
    __tablename__ = "ponds_movements"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    source_pond_id = Column(BigInteger, ForeignKey("ponds.id"))
    destiny_pond_id = Column(BigInteger, ForeignKey("ponds.id"))
    fish_id = Column(BigInteger, ForeignKey("fish.id"))
    lot_id = Column(BigInteger, ForeignKey("lots.id"))
    fish_quantity = Column(Integer, nullable=False)
    movement_reason = Column(String(50), nullable=False)
    movement_time = Column(TIMESTAMP, nullable=False)
    folio = Column(Integer)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
