from sqlalchemy import Column, BigInteger, Numeric, Text, TIMESTAMP, ForeignKey
from app.db.session import Base

class SamplingRecord(Base):
    __tablename__ = "sampling_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(BigInteger, ForeignKey("sampling_sessions.id"), nullable=False)
    fish_id = Column(BigInteger, ForeignKey("fish.id"))
    lot_id = Column(BigInteger, ForeignKey("lots.id"))
    weight = Column(Numeric(10, 3))
    length = Column(Numeric(10, 3))
    notes = Column(Text)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
