from sqlalchemy import Column, BigInteger, Date, Text, TIMESTAMP, ForeignKey
from app.db.session import Base

class SamplingSession(Base):
    __tablename__ = "sampling_sessions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False)
    registry_date = Column(Date, nullable=False)
    notes = Column(Text)
    closed_at = Column(TIMESTAMP)  # si no es None → sesión cerrada
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
