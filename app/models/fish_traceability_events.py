from sqlalchemy import Column, BigInteger, String, TIMESTAMP, JSON
from app.db.session import Base

class FishTraceabilityEvent(Base):
    __tablename__ = "fish_traceability_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    fish_id = Column(BigInteger, nullable=False)
    event_type = Column(String(40), nullable=False)
    event_time = Column(TIMESTAMP, nullable=False)
    source_table = Column(String(80), nullable=False)
    source_id = Column(BigInteger, nullable=False)
    payload = Column(JSON, nullable=False)
