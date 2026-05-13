from sqlalchemy import Column, BigInteger, String, Text, TIMESTAMP, ForeignKey
from sqlalchemy.orm import validates
from app.db.session import Base

class Fish(Base):
    __tablename__ = "fish"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    internal_id = Column(String(120), nullable=False)
    secondary_internal_id = Column(String(120))
    lot_id = Column(BigInteger, ForeignKey("lots.id"), nullable=False)
    sex = Column(String(20))
    state = Column(String(30), default='alive')
    registration_time = Column(TIMESTAMP)
    devious_time = Column(TIMESTAMP)
    date_of_death = Column(TIMESTAMP)
    depuration_start_time = Column(TIMESTAMP)
    mortality_id = Column(String(120))
    notes = Column(Text)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)

    @staticmethod
    def _normalize_pit_value(value):
        if value is None:
            return None
        normalized = str(value).strip().upper()
        return normalized or None

    @validates("internal_id", "secondary_internal_id")
    def _normalize_pit_fields(self, _key, value):
        return self._normalize_pit_value(value)
