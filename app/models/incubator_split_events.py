from sqlalchemy import Column, BigInteger, Text, TIMESTAMP, ForeignKey
from app.db.session import Base


class IncubatorSplitEvent(Base):
    """Registra cada partición de una incubadora en N sub-unidades."""

    __tablename__ = "incubator_split_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source_unit_id = Column(BigInteger, ForeignKey("incubator_units.id"), nullable=False, index=True)
    split_datetime = Column(TIMESTAMP, nullable=False)
    notes = Column(Text)
    created_at = Column(TIMESTAMP)
